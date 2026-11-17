"""Request-scoped header capture and Databricks client construction."""

from __future__ import annotations

import contextvars
import os

from databricks.sdk import WorkspaceClient

#: Populated per-request by the middleware in :mod:`server.app`.
header_store: contextvars.ContextVar[dict] = contextvars.ContextVar("header_store")

USER_TOKEN_HEADER = "x-forwarded-access-token"


def get_workspace_client() -> WorkspaceClient:
    """Client authenticated as the app's own service principal."""
    return WorkspaceClient()


def get_user_authenticated_workspace_client() -> WorkspaceClient:
    """Client authenticated on behalf of the calling user.

    Databricks Apps forwards the end user's token in the
    ``x-forwarded-access-token`` header when the app declares
    ``user_api_scopes``. Outside the Apps runtime this falls back to whatever
    local credentials the developer has configured.

    Raises:
        ValueError: when running inside Apps but the header is absent, which
            means the caller did not authenticate as a user.
    """
    if "DATABRICKS_APP_NAME" not in os.environ:
        return WorkspaceClient()

    token = header_store.get({}).get(USER_TOKEN_HEADER)
    if not token:
        raise ValueError(
            f"No user token found in request headers ({USER_TOKEN_HEADER}). "
            "MCP callers must authenticate via OAuth; personal access tokens are "
            "not supported for MCP requests to Databricks Apps."
        )
    return WorkspaceClient(token=token, auth_type="pat")
