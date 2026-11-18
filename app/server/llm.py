"""Thin client for the 50/50 question-ranking model service.

Isolated from :mod:`server.ranking` so the ranking logic stays testable without
Databricks auth, and isolated from :mod:`server.tools` so the transport can be
swapped without touching the tool surface.
"""

from __future__ import annotations

import os

from databricks.sdk import WorkspaceClient

#: Injected by the bundle via app.yaml `valueFrom: ranking-model-service`, which
#: resolves to the model service's full name, <catalog>.<schema>.<id>.
MODEL_SERVICE_ENV_VAR = "MODEL_SERVICE"
DEFAULT_MAX_TOKENS = 1024

#: Unity Gateway's OpenAI-compatible Chat Completions API. A model service is
#: addressed by passing its full name as `model`.
CHAT_COMPLETIONS_PATH = "/ai-gateway/mlflow/v1/chat/completions"


def model_service_name() -> str:
    """Return the configured model service's full name.

    Raises:
        RuntimeError: if the app was deployed without the model-service resource
            wired up, which would otherwise surface as a confusing API error.
    """
    name = (os.environ.get(MODEL_SERVICE_ENV_VAR) or "").strip()
    if not name:
        raise RuntimeError(
            f"{MODEL_SERVICE_ENV_VAR} is not set. The bundle wires this from the app's "
            "model-service resource; check resources/app.yml and app/app.yaml."
        )
    return name


def _first_message_content(response: object) -> str:
    """Extract the assistant text from a Chat Completions response dict.

    `content` is a plain string from both default models. A list of content
    blocks is also accepted, keeping only the `text` blocks, so a reasoning
    block can never be mistaken for the answer.
    """
    choices = response.get("choices") if isinstance(response, dict) else None
    for choice in choices or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if content:
            return str(content)
    return ""


def complete(prompt: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Send a single-turn chat completion to the 50/50 model service.

    Uses the app's own service principal (``WorkspaceClient()`` picks up the
    auto-injected ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET``), which
    holds ``EXECUTE`` on the model service through the bundle's app resource.
    The service sends each call to one of its two models, 50/50.
    """
    client = WorkspaceClient()
    response = client.api_client.do(
        "POST",
        CHAT_COMPLETIONS_PATH,
        body={
            "model": model_service_name(),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
        },
    )
    return _first_message_content(response)
