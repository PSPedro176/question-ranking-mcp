# Databricks notebook source
# MAGIC %md
# MAGIC # Register the question-ranking MCP server in Unity Catalog
# MAGIC
# MAGIC Idempotently creates (1) a schema-level Unity Catalog **HTTP connection** pointing at
# MAGIC the deployed Databricks App's `/mcp` endpoint and (2) a **UC MCP service** that
# MAGIC references that connection.
# MAGIC
# MAGIC This runs as a job rather than as a declarative `resources.mcp_services` block on
# MAGIC purpose. Databricks currently documents that *"Registering Genie, Databricks Apps, or
# MAGIC Unity Catalog entity sources as your own MCP isn't supported"*, so this call may be
# MAGIC rejected by the platform. As a job, a rejection fails only this run; as a declarative
# MAGIC resource it would fail every `databricks bundle deploy`.
# MAGIC
# MAGIC Defaults to a **dry run**. Set the bundle variable `register_uc_mcp_service=true`
# MAGIC (or the `register` job parameter) to actually mutate Unity Catalog.

# COMMAND ----------

import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError
from databricks.sdk.service.catalog import (
    ConnectionType,
    McpService,
    McpServiceConfig,
    McpServiceConfigSourceConnection,
)

# COMMAND ----------

for name, default in [
    ("catalog", "prod_ai"),
    ("schema", "question-ranking-mcp"),
    ("app_name", "mcp-question-ranking"),
    ("connection_name", "question_ranking_mcp_http"),
    ("mcp_service_id", "question_ranking"),
    ("register", "false"),
    ("bearer_token_secret", ""),
    ("base_path", "/mcp"),
]:
    dbutils.widgets.text(name, default)  # noqa: F821 - provided by the Databricks runtime

CATALOG = dbutils.widgets.get("catalog").strip()  # noqa: F821
SCHEMA = dbutils.widgets.get("schema").strip()  # noqa: F821
APP_NAME = dbutils.widgets.get("app_name").strip()  # noqa: F821
CONNECTION_NAME = dbutils.widgets.get("connection_name").strip()  # noqa: F821
MCP_SERVICE_ID = dbutils.widgets.get("mcp_service_id").strip()  # noqa: F821
BEARER_TOKEN_SECRET = dbutils.widgets.get("bearer_token_secret").strip()  # noqa: F821
BASE_PATH = dbutils.widgets.get("base_path").strip() or "/mcp"  # noqa: F821
REGISTER = dbutils.widgets.get("register").strip().lower() == "true"  # noqa: F821

PARENT = f"schemas/{CATALOG}.{SCHEMA}"
CONNECTION_RESOURCE = f"connections/{CATALOG}.{SCHEMA}.{CONNECTION_NAME}"
MCP_RESOURCE = f"mcp-services/{CATALOG}.{SCHEMA}.{MCP_SERVICE_ID}"

w = WorkspaceClient()

# COMMAND ----------

# MAGIC %md ## Resolve the deployed app's MCP URL

app_url = None
try:
    app_url = (w.apps.get(name=APP_NAME).url or "").rstrip("/")
except DatabricksError as exc:
    print(f"WARNING: could not read app {APP_NAME!r}: {exc}")

if app_url and not app_url.startswith("http"):
    app_url = f"https://{app_url}"

mcp_endpoint = f"{app_url}{BASE_PATH}" if app_url else None

print(
    json.dumps(
        {
            "plan": {
                "parent": PARENT,
                "connection": CONNECTION_RESOURCE,
                "mcp_service": MCP_RESOURCE,
                "app": APP_NAME,
                "app_url": app_url,
                "mcp_endpoint": mcp_endpoint,
                "register": REGISTER,
            }
        },
        indent=2,
    )
)

# COMMAND ----------

if not REGISTER:
    print(
        "DRY RUN: register=false, so nothing was created.\n"
        "Set the bundle variable register_uc_mcp_service=true (or pass register=true) to "
        "attempt registration.\n"
        "NOTE: Databricks documents Databricks Apps as an unsupported MCP registration "
        "source, so this may be rejected. The supported consumption path is connecting your "
        f"MCP client directly to {mcp_endpoint or '<app-url>/mcp'}."
    )
    dbutils.notebook.exit("dry-run")  # noqa: F821

if not app_url:
    raise RuntimeError(
        f"App {APP_NAME!r} has no URL yet. Deploy and start the app before registering it."
    )

# COMMAND ----------

# MAGIC %md ## Step 1 — schema-level HTTP connection (idempotent)

options = {"host": app_url, "port": "443", "base_path": BASE_PATH}
secrets = None

if BEARER_TOKEN_SECRET:
    # Format: "<scope>/<key>" -> the UC secret FQN the connection should read.
    scope, _, key = BEARER_TOKEN_SECRET.partition("/")
    if not key:
        raise ValueError(
            f"bearer_token_secret must be '<scope>/<key>', got {BEARER_TOKEN_SECRET!r}"
        )
    secrets = {"bearer_token": f"secrets/{scope}/{key}"}
else:
    print(
        "WARNING: no bearer_token_secret supplied. 'bearer_token' is a required option for "
        "a UC HTTP connection, so creation will likely fail. Databricks Apps MCP endpoints "
        "require OAuth and do not accept static bearer tokens or PATs, which is the "
        "underlying reason Apps-hosted MCP registration is unsupported."
    )

try:
    existing = w.connections.get(name=CONNECTION_RESOURCE)
    print(f"Connection already exists, reusing: {existing.full_name or CONNECTION_RESOURCE}")
except DatabricksError as exc:
    if "does not exist" not in str(exc).lower() and "not found" not in str(exc).lower():
        raise
    print(f"Creating HTTP connection {CONNECTION_RESOURCE} -> {app_url}{BASE_PATH}")
    w.connections.create(
        name=CONNECTION_NAME,
        connection_type=ConnectionType.HTTP,
        options=options,
        parent=PARENT,
        secrets=secrets,
        comment="HTTP connection to the question-ranking MCP server on Databricks Apps.",
    )

# COMMAND ----------

# MAGIC %md ## Step 2 — UC MCP service (idempotent)

try:
    existing_mcp = w.ai_gateway.get_mcp_service(name=MCP_RESOURCE)
    print(f"MCP service already exists, nothing to do: {existing_mcp.name}")
    dbutils.notebook.exit("already-registered")  # noqa: F821
except DatabricksError as exc:
    if "does not exist" not in str(exc).lower() and "not found" not in str(exc).lower():
        raise

print(f"Creating MCP service {MCP_RESOURCE}")
try:
    created = w.ai_gateway.create_mcp_service(
        mcp_service=McpService(
            comment="Question-ranking MCP server hosted on Databricks Apps.",
            config=McpServiceConfig(
                source_connection=McpServiceConfigSourceConnection(name=CONNECTION_RESOURCE),
            ),
        ),
        parent=PARENT,
        mcp_service_id=MCP_SERVICE_ID,
    )
    print(f"Registered: {created.name}")
    print(
        "Consume via: "
        f"https://<workspace-hostname>/ai-gateway/mcp-services/{CATALOG}.{SCHEMA}.{MCP_SERVICE_ID}"
    )
except DatabricksError as exc:
    raise RuntimeError(
        f"Unity Catalog rejected the MCP service registration: {exc}\n\n"
        "This is the documented limitation, not a bug in this bundle: "
        '"You can register external MCP servers. Registering Genie, Databricks Apps, or '
        "Unity Catalog entity sources as your own MCP isn't supported.\"\n"
        "The supported path is to connect your MCP client directly to "
        f"{app_url}{BASE_PATH}. See the README section 'Unity Catalog registration'."
    ) from exc
