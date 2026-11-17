"""Entry point for the question-ranking MCP server.

Binds ``0.0.0.0`` on the port the Databricks Apps runtime injects as
``DATABRICKS_APP_PORT`` (not ``PORT``), defaulting to 8000 for local runs.
"""

from __future__ import annotations

import os

import uvicorn

DEFAULT_PORT = 8000


def main() -> None:
    """Start the ASGI server."""
    uvicorn.run(
        "server.app:combined_app",
        host="0.0.0.0",
        port=int(os.environ.get("DATABRICKS_APP_PORT", DEFAULT_PORT)),
    )


if __name__ == "__main__":
    main()
