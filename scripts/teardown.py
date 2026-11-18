"""Delete the UC MCP service, then the UC connection, ignoring only confirmed NOT_FOUND.

Both live in Unity Catalog outside what `bundle destroy` can order correctly (the connection is
created by a job, and a schema that still contains it cannot be dropped), so `make destroy` runs
this first. Any other failure (permissions, network, ...) is propagated.
"""

import argparse
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound


def delete_if_exists(label: str, delete) -> None:
    try:
        delete()
        print(f"Deleted {label}")
    except NotFound:
        print(f"{label} not found; nothing to delete")


def main(argv: list[str] | None = None, client: WorkspaceClient | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--service", required=True)
    ap.add_argument("--connection", required=True)
    args = ap.parse_args(argv)

    w = client or WorkspaceClient(profile=args.profile)
    prefix = f"{args.catalog}.{args.schema}"
    delete_if_exists(
        f"MCP service {prefix}.{args.service}",
        lambda: w.ai_gateway.delete_mcp_service(name=f"mcp-services/{prefix}.{args.service}"),
    )
    delete_if_exists(
        f"connection {prefix}.{args.connection}",
        lambda: w.connections.delete(name=f"{prefix}.{args.connection}"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
