"""Thin client for the 50/50 question-ranking serving endpoint.

Isolated from :mod:`server.ranking` so the ranking logic stays testable without
Databricks auth, and isolated from :mod:`server.tools` so the transport can be
swapped without touching the tool surface.
"""

from __future__ import annotations

import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

#: Injected by the bundle via app.yaml `valueFrom: serving-endpoint`.
ENDPOINT_ENV_VAR = "SERVING_ENDPOINT"
DEFAULT_MAX_TOKENS = 1024


def endpoint_name() -> str:
    """Return the configured serving endpoint name.

    Raises:
        RuntimeError: if the app was deployed without the endpoint resource
            wired up, which is otherwise a confusing 404 at query time.
    """
    name = (os.environ.get(ENDPOINT_ENV_VAR) or "").strip()
    if not name:
        raise RuntimeError(
            f"{ENDPOINT_ENV_VAR} is not set. The bundle wires this from the app's "
            "serving-endpoint resource; check resources/app.yml and app/app.yaml."
        )
    return name


def _first_message_content(response: object) -> str:
    """Extract assistant text from a serving response, tolerating shape drift."""
    choices = getattr(response, "choices", None) or []
    for choice in choices:
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if content:
            return str(content)
    return ""


def complete(prompt: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Send a single-turn chat completion to the 50/50 endpoint.

    Uses the app's own service principal (``WorkspaceClient()`` picks up the
    auto-injected ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET``), which
    holds ``CAN_QUERY`` on the endpoint via the bundle's app resource block.
    Each call lands on one of the two served models per the 50/50 traffic split.
    """
    client = WorkspaceClient()
    response = client.serving_endpoints.query(
        name=endpoint_name(),
        messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt)],
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return _first_message_content(response)
