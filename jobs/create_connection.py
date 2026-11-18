"""Idempotently create the UC HTTP connection that backs the MCP service.

Runs as a serverless job during `databricks bundle deploy` (connections are not a bundle
resource type). The connection authenticates to the app with OAuth M2M as the app's own
service principal: a fresh OAuth secret is minted here and handed straight to Unity
Catalog, so it is never logged, committed or stored anywhere else.
"""

import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import ConnectionType

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--app_name", required=True)
parser.add_argument("--connection_name", required=True)
args = parser.parse_args()

full_name = f"{args.catalog}.{args.schema}.{args.connection_name}"
w = WorkspaceClient()

try:
    w.connections.get(name=full_name)
    print(f"Connection {full_name} already exists; nothing to do.")
    exists = True
except NotFound:
    exists = False

if not exists:
    app = w.apps.get(name=args.app_name)
    host = f"https://{app.url}" if not app.url.startswith("http") else app.url
    host = host.rstrip("/")
    print(f"App {app.name}: url={host} sp_client_id={app.service_principal_client_id}")

    secret = w.service_principal_secrets_proxy.create(
        service_principal_id=str(app.service_principal_id)
    ).secret

    w.connections.create(
        name=args.connection_name,
        connection_type=ConnectionType.HTTP,
        parent=f"schemas/{args.catalog}.{args.schema}",
        comment="OAuth M2M connection to the hello-mcp Databricks App.",
        options={
            "host": host,
            "port": "443",
            "base_path": "/mcp",
            "client_id": app.service_principal_client_id,
            "client_secret": secret,
            "token_endpoint": f"{w.config.host.rstrip('/')}/oidc/v1/token",
            "oauth_scope": "all-apis",
            "oauth_credential_exchange_method": "header_and_body",
        },
    )
    print(f"Created connection {full_name} -> {host}/mcp")
