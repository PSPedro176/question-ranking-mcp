"""Hello-world MCP server: one tool, streamable HTTP at /mcp, stateless."""

from __future__ import annotations

import os

import uvicorn
from fastmcp import FastMCP

MCP_PATH = "/mcp"
DEFAULT_PORT = 8000

mcp = FastMCP(name="hello-mcp")


def say_hello(name: str = "world") -> str:
    """Greet someone by name.

    Args:
        name: Who to greet. Defaults to "world".
    """
    return f"Hello, {name}!"


# Registered without rebinding the name, so say_hello stays a plain callable.
mcp.tool(say_hello)

# Stateless: gateway and Playground clients do not always send an mcp-session-id.
app = mcp.http_app(path=MCP_PATH, stateless_http=True)


def main() -> None:
    """Serve on 0.0.0.0 at the port the Databricks Apps runtime injects."""
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("DATABRICKS_APP_PORT", DEFAULT_PORT)),
    )


if __name__ == "__main__":
    main()
