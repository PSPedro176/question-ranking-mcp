"""Idempotently create or repair the UC HTTP connection that backs the MCP service.

Runs as a serverless job during `databricks bundle deploy` (connections are not a bundle
resource type). The connection authenticates to the app with OAuth M2M as the app's own
service principal. A fresh OAuth secret is minted only when the connection is missing or
stale (host, base path, client id or token endpoint differ from the app as it is now), is
handed straight to Unity Catalog, and is revoked again if that call fails. Secret values are
never logged.
"""

from __future__ import annotations

import argparse
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound
from databricks.sdk.service.catalog import ConnectionType

BASE_PATH = "/mcp"


def desired_options(w: WorkspaceClient, app) -> dict[str, str]:
    """Connection options (minus the secret) that match the app as it exists now."""
    host = app.url if app.url.startswith("http") else f"https://{app.url}"
    return {
        "host": host.rstrip("/"),
        "port": "443",
        "base_path": BASE_PATH,
        "client_id": app.service_principal_client_id,
        "token_endpoint": f"{w.config.host.rstrip('/')}/oidc/v1/token",
        "oauth_scope": "all-apis",
        "oauth_credential_exchange_method": "header_and_body",
    }


def is_current(existing: dict[str, str] | None, desired: dict[str, str]) -> bool:
    existing = existing or {}
    return all(existing.get(key) == value for key, value in desired.items())


def main(argv: list[str] | None = None, client: WorkspaceClient | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--app_name", required=True)
    parser.add_argument("--connection_name", required=True)
    args = parser.parse_args(argv)

    w = client or WorkspaceClient()
    full_name = f"{args.catalog}.{args.schema}.{args.connection_name}"
    app = w.apps.get(name=args.app_name)
    desired = desired_options(w, app)

    try:
        existing = w.connections.get(name=full_name)
    except NotFound:
        existing = None

    if existing is not None and is_current(existing.options, desired):
        print(f"Connection {full_name} is current ({desired['host']}); nothing to do.")
        return 0

    secret_id = secret = None
    try:
        minted = w.service_principal_secrets_proxy.create(
            service_principal_id=str(app.service_principal_id)
        )
        secret_id, secret = minted.id, minted.secret
        options = {**desired, "client_secret": secret}
        if existing is None:
            print(f"Creating connection {full_name} -> {desired['host']}{BASE_PATH}")
            w.connections.create(
                name=args.connection_name,
                connection_type=ConnectionType.HTTP,
                parent=f"schemas/{args.catalog}.{args.schema}",
                comment="OAuth M2M connection to the hello-mcp Databricks App.",
                options=options,
            )
        else:
            print(f"Connection {full_name} is stale; updating it -> {desired['host']}{BASE_PATH}")
            w.connections.update(name=full_name, options=options)
    except Exception as exc:
        if secret_id:
            try:
                w.service_principal_secrets_proxy.delete(
                    service_principal_id=str(app.service_principal_id), secret_id=secret_id
                )
                print("Revoked the OAuth secret minted for the failed attempt.", file=sys.stderr)
            except DatabricksError as revoke_exc:
                print(
                    f"WARNING: could not revoke secret {secret_id}: {type(revoke_exc).__name__}",
                    file=sys.stderr,
                )
        message = str(exc)
        if secret:
            message = message.replace(secret, "***")
        print(f"ERROR: [{type(exc).__name__}] {message}", file=sys.stderr)
        return 1
    print(f"Connection {full_name} is ready.")
    return 0


if __name__ == "__main__":
    # Serverless treats SystemExit(0) as a failed run, so only raise on failure.
    rc = main()
    if rc:
        raise SystemExit(rc)
