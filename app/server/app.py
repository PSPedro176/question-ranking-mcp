"""FastAPI + FastMCP application for the question-ranking MCP server.

The MCP protocol is served at ``/mcp`` over streamable HTTP in stateless mode.
Stateless matters here: several Databricks MCP clients (the AI Playground and
the Assistant among them) do not send an ``mcp-session-id`` header, and a
stateful server rejects their follow-up requests with HTTP 400. It also lets the
app scale horizontally, since no request depends on a prior handshake landing on
the same replica.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastmcp import FastMCP

from server.tools import load_tools
from server.utils import header_store

MCP_PATH = "/mcp"

mcp_server = FastMCP(name="question-ranking-mcp")
load_tools(mcp_server)

mcp_app = mcp_server.http_app(path=MCP_PATH, stateless_http=True)

api = FastAPI(lifespan=mcp_app.lifespan)


@api.get("/health", include_in_schema=False)
async def health() -> dict:
    """Liveness probe for the Databricks Apps runtime."""
    return {"status": "healthy", "mcp_path": MCP_PATH}


@api.get("/", include_in_schema=False)
async def root() -> dict:
    """Human-facing landing response pointing at the MCP endpoint."""
    return {
        "name": "question-ranking-mcp",
        "mcp_endpoint": MCP_PATH,
        "transport": "streamable-http",
    }


combined_app = FastAPI(
    title="question-ranking-mcp",
    description="MCP server that ranks questions using a 50/50 open-source model endpoint.",
    routes=[*mcp_app.routes, *api.routes],
    lifespan=mcp_app.lifespan,
)


@combined_app.middleware("http")
async def capture_headers(request: Request, call_next):
    """Stash request headers so tools can read the on-behalf-of-user token."""
    header_store.set(dict(request.headers))
    return await call_next(request)
