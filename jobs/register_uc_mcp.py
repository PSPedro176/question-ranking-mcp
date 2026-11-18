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
from databricks.sdk.errors import DatabricksError, NotFound
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

# Two forms of the same connection, deliberately kept apart:
#   CONNECTION_FULL_NAME - three-level name. `connections.get()` interpolates it straight
#       into /api/2.1/unity-catalog/connections/{name}, so the `connections/` prefix must
#       NOT be included or the request path doubles up and the lookup always 404s.
#   CONNECTION_RESOURCE  - resource name. Required by McpServiceConfigSourceConnection.
CONNECTION_FULL_NAME = f"{CATALOG}.{SCHEMA}.{CONNECTION_NAME}"
CONNECTION_RESOURCE = f"connections/{CONNECTION_FULL_NAME}"
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
                "connection": CONNECTION_FULL_NAME,
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


def resolve_uc_secret(reference: str) -> str:
    """Validate the FORMAT of a Unity Catalog Secret reference. Purely local.

    Deliberately no `secrets_uc.get_secret` preflight: that call needs secret
    ownership, metastore admin or MANAGE, so it would reject a valid secret held with
    only READ SECRET. `connections.create` validates the reference itself; its
    rejection is surfaced below with an actionable message.

    `connections.create(secrets=...)` maps a connection option name to a **UC Secret
    fully qualified name** - a three-level `catalog.schema.secret`. It is NOT a workspace
    secret-scope path: `<scope>/<key>` and `secrets/<scope>/<key>` are a different,
    incompatible namespace and are rejected here rather than passed on to fail opaquely.
    """
    if "/" in reference:
        raise ValueError(
            f"bearer_token_secret must be a Unity Catalog Secret fully qualified name "
            f"'<catalog>.<schema>.<secret>', got {reference!r}.\n"
            "A workspace secret-scope path ('<scope>/<key>' or 'secrets/<scope>/<key>') is a "
            "different namespace and is not accepted by the Unity Catalog connections API. "
            "Create a UC Secret instead, then pass its three-level name."
        )
    parts = reference.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"bearer_token_secret must have exactly three non-empty parts "
            f"'<catalog>.<schema>.<secret>', got {reference!r}."
        )
    return reference


if BEARER_TOKEN_SECRET:
    secrets = {"bearer_token": resolve_uc_secret(BEARER_TOKEN_SECRET)}
else:
    print(
        "WARNING: no bearer_token_secret supplied. 'bearer_token' is a required option for "
        "a UC HTTP connection that uses bearer-token authentication, so creation will "
        "likely fail. Pass a Unity Catalog Secret as bearer_token_secret."
    )

try:
    existing = w.connections.get(name=CONNECTION_FULL_NAME)
    print(f"Connection already exists, reusing: {existing.full_name or CONNECTION_FULL_NAME}")
except NotFound:
    print(f"Creating HTTP connection {CONNECTION_FULL_NAME} -> {app_url}{BASE_PATH}")
    try:
        w.connections.create(
            name=CONNECTION_NAME,
            connection_type=ConnectionType.HTTP,
            options=options,
            parent=PARENT,
            secrets=secrets,
            comment="HTTP connection to the question-ranking MCP server on Databricks Apps.",
        )
    except DatabricksError as exc:
        hint = (
            f"If the error concerns the bearer token, check that UC Secret "
            f"{BEARER_TOKEN_SECRET!r} exists and that this identity has USE CATALOG and "
            "USE SCHEMA on its parents plus READ SECRET on it."
            if BEARER_TOKEN_SECRET
            else "No bearer_token_secret was supplied; 'bearer_token' is a required option "
            "for a bearer-token HTTP connection."
        )
        raise RuntimeError(
            f"Unity Catalog rejected creating connection {CONNECTION_FULL_NAME}: "
            f"[{type(exc).__name__}] {exc}\n{hint}\n"
            f"Creating a connection also needs CREATE CONNECTION on schema {CATALOG}.{SCHEMA}."
        ) from exc

# COMMAND ----------

# MAGIC %md ## Step 2 — UC MCP service (idempotent)

try:
    existing_mcp = w.ai_gateway.get_mcp_service(name=MCP_RESOURCE)
    print(f"MCP service already exists, nothing to do: {existing_mcp.name}")
    dbutils.notebook.exit("already-registered")  # noqa: F821
except NotFound:
    pass

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
