"""Offline integration tests: the real FastAPI + FastMCP app over HTTP.

These drive ``server.app.combined_app`` through the actual streamable-HTTP MCP
protocol, with the LLM and ``WorkspaceClient`` stubbed. No network, no
Databricks auth. They cover the boundaries the pure ranking tests cannot see -
which is exactly where ``best_question`` once broke without any test noticing.
"""

from __future__ import annotations

import json
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import llm, tools, utils
from server.app import combined_app

MODEL_SERVICE = "prod_ai.question-ranking-mcp.question_ranking_router"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
GOOD_RESPONSE = (
    '[{"index": 1, "score": 2, "rationale": "too vague"},'
    ' {"index": 2, "score": 9, "rationale": "specific and answerable"}]'
)
QUESTIONS = ["What about stuff?", "What drove Q3 churn in EMEA?"]


@pytest.fixture(scope="module")
def client():
    """One client, one app lifespan, shared by every test in this module."""
    with TestClient(combined_app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _model_service_env(monkeypatch):
    monkeypatch.setenv("MODEL_SERVICE", MODEL_SERVICE)


@pytest.fixture
def llm_returns(monkeypatch):
    """Stub the 50/50 model service with a canned response."""

    def _set(text: str):
        monkeypatch.setattr(llm, "complete", lambda prompt, **kw: text)

    return _set


def _rpc(client, method, params=None, headers=None, rid=1):
    """POST one JSON-RPC message to /mcp and decode the SSE-framed reply."""
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post("/mcp", headers={**MCP_HEADERS, **(headers or {})}, json=body)
    assert response.status_code == 200, response.text
    frames = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    return json.loads(frames[-1])


def _call_tool(client, name, arguments, headers=None):
    reply = _rpc(client, "tools/call", {"name": name, "arguments": arguments}, headers)
    return reply["result"]["structuredContent"]


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


def test_initialize_then_follow_up_without_session_id(client):
    """Stateless mode: a follow-up with NO mcp-session-id must still succeed.

    Several Databricks MCP clients never send the header; a stateful server
    would reject this second request with HTTP 400.
    """
    init = _rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    )
    assert init["result"]["serverInfo"]["name"] == "question-ranking-mcp"

    listed = _rpc(client, "tools/list", rid=2)  # deliberately no session header
    names = sorted(tool["name"] for tool in listed["result"]["tools"])
    assert names == ["best_question", "rank_questions", "whoami"]


def test_health_is_served_within_the_mcp_lifespan(client):
    assert client.get("/health").json() == {"status": "healthy", "mcp_path": "/mcp"}


def test_tool_functions_stay_callable_after_registration():
    """Guards the FastMCP decorator trap directly, independent of version.

    `@mcp_server.tool` replaces the function with a non-callable FunctionTool
    on some FastMCP versions; registering must leave the module names callable.
    """
    assert all(callable(fn) for fn in (tools.rank_questions, tools.best_question, tools.whoami))


# --------------------------------------------------------------------------- #
# Ranking tools through real MCP registration
# --------------------------------------------------------------------------- #


def test_rank_questions_success(client, llm_returns):
    llm_returns(GOOD_RESPONSE)
    result = _call_tool(client, "rank_questions", {"questions": QUESTIONS})
    assert [r["question"] for r in result["ranked"]] == [QUESTIONS[1], QUESTIONS[0]]
    assert result["model_scored"] == 2
    assert result["model_service"] == MODEL_SERVICE


def test_best_question_success(client, llm_returns):
    """best_question calls rank_questions internally - the call that once broke."""
    llm_returns(GOOD_RESPONSE)
    result = _call_tool(client, "best_question", {"questions": QUESTIONS})
    assert result == {
        "question": QUESTIONS[1],
        "score": 9.0,
        "rationale": "specific and answerable",
        "runner_up": QUESTIONS[0],
        "model_service": MODEL_SERVICE,
    }


def test_best_question_with_one_question_has_no_runner_up(client, llm_returns):
    llm_returns('[{"index": 1, "score": 6, "rationale": "fine"}]')
    assert _call_tool(client, "best_question", {"questions": ["Only?"]})["runner_up"] is None


@pytest.mark.parametrize("tool", ["rank_questions", "best_question"])
def test_ranking_tools_report_model_service_failure(client, monkeypatch, tool):
    def boom(prompt, **kw):
        raise RuntimeError("model service unavailable")

    monkeypatch.setattr(llm, "complete", boom)
    result = _call_tool(client, tool, {"questions": QUESTIONS})
    assert result["error"] == "model_service_query_failed"
    assert "model service unavailable" in result["message"]


@pytest.mark.parametrize("tool", ["rank_questions", "best_question"])
def test_ranking_tools_report_missing_model_service_config(client, monkeypatch, llm_returns, tool):
    llm_returns(GOOD_RESPONSE)
    monkeypatch.delenv("MODEL_SERVICE")
    result = _call_tool(client, tool, {"questions": QUESTIONS})
    assert result["error"] == "model_service_query_failed"


@pytest.mark.parametrize("tool", ["rank_questions", "best_question"])
def test_ranking_tools_reject_invalid_input(client, llm_returns, tool):
    llm_returns(GOOD_RESPONSE)
    assert _call_tool(client, tool, {"questions": ["   "]})["error"] == "invalid_questions"


def test_malformed_model_numerics_return_a_structured_result(client, llm_returns):
    """NaN/overflow once escaped as an opaque MCP error; now they fall back."""
    llm_returns('[{"index": NaN, "score": 9}, {"index": 1e999, "score": 1e999}]')
    result = _call_tool(client, "rank_questions", {"questions": QUESTIONS})
    assert result["model_scored"] == 0
    assert [r["score"] for r in result["ranked"]] == [5.0, 5.0]


# --------------------------------------------------------------------------- #
# Model-service client (llm.py)
# --------------------------------------------------------------------------- #


class _RecordingApiClient:
    """Captures the request llm.complete sends and returns a canned response."""

    def __init__(self, response):
        self.response, self.calls = response, []

    def do(self, method, path=None, body=None, **kwargs):
        self.calls.append({"method": method, "path": path, "body": body})
        return self.response


def _chat_response(content, **message_extra):
    """Shape copied from live Unity Gateway Chat Completions responses."""
    return {
        "choices": [{"message": {"role": "assistant", "content": content, **message_extra}}],
        "id": "x",
        "model": "m",
        "object": "chat.completion",
        "created": 0,
        "usage": {},
    }


def test_complete_calls_the_model_service_by_full_name(monkeypatch):
    api = _RecordingApiClient(_chat_response('[{"index": 1, "score": 7}]'))
    monkeypatch.setattr(llm, "WorkspaceClient", lambda: SimpleNamespace(api_client=api))

    assert llm.complete("rank these") == '[{"index": 1, "score": 7}]'
    (call,) = api.calls
    assert (call["method"], call["path"]) == ("POST", "/ai-gateway/mlflow/v1/chat/completions")
    assert call["body"]["model"] == MODEL_SERVICE
    assert call["body"]["messages"] == [{"role": "user", "content": "rank these"}]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        # gpt-oss returns its reasoning separately; only `content` is the answer.
        (_chat_response("[1]", reasoning_content="thinking..."), "[1]"),
        (
            _chat_response([{"type": "reasoning", "text": "x"}, {"type": "text", "text": "[2]"}]),
            "[2]",
        ),
        (_chat_response(None), ""),
        ({"choices": []}, ""),
        ({}, ""),
        ([], ""),
    ],
)
def test_first_message_content_tolerates_response_shapes(response, expected):
    assert llm._first_message_content(response) == expected


# --------------------------------------------------------------------------- #
# On-behalf-of-user token isolation
# --------------------------------------------------------------------------- #


class _FakeWorkspaceClient:
    """Stands in for WorkspaceClient; echoes back the token it was built with."""

    def __init__(self, token=None, auth_type=None):
        self.token = token
        self.current_user = SimpleNamespace(
            me=lambda: SimpleNamespace(display_name="Test User", user_name=f"user-for-{token}")
        )


def test_concurrent_requests_each_see_only_their_own_token(client, monkeypatch):
    """Token isolation under real concurrency, with no wall-clock assertion.

    A barrier sits between "middleware stored this request's headers" and "tool
    reads the token". It only releases once EVERY request is parked there, so all
    of them have stored their headers before any of them reads one. If header
    storage leaked across requests, they would all read the last writer's token.
    The barrier timeout is a deadlock guard only; if requests cannot run
    concurrently it breaks, and the test fails on the error, not on timing.
    """
    tokens = ["tok-a", "tok-b", "tok-c", "tok-d", None]  # None: no forwarded token
    barrier = threading.Barrier(len(tokens), timeout=30)
    read_token = utils.get_user_authenticated_workspace_client

    def read_after_everyone_is_in_flight():
        barrier.wait()
        return read_token()

    monkeypatch.setenv("DATABRICKS_APP_NAME", "mcp-question-ranking")
    monkeypatch.setattr(utils, "WorkspaceClient", _FakeWorkspaceClient)
    monkeypatch.setattr(
        utils, "get_user_authenticated_workspace_client", read_after_everyone_is_in_flight
    )

    def whoami_as(token):
        headers = {utils.USER_TOKEN_HEADER: token} if token else {}
        return _call_tool(client, "whoami", {}, headers=headers)["user"]

    with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
        users = list(pool.map(whoami_as, tokens))

    for token, user in zip(tokens[:-1], users[:-1], strict=True):
        assert user.get("user_name") == f"user-for-{token}", user
    assert "No user token found" in users[-1]["error"], users[-1]


# --------------------------------------------------------------------------- #
# Dependency drift guard
# --------------------------------------------------------------------------- #

APP_LOCK = Path(__file__).resolve().parents[1] / "app" / "uv.lock"


@pytest.mark.parametrize(
    "package", ["fastmcp", "mcp", "fastapi", "starlette", "uvicorn", "databricks-sdk", "pydantic"]
)
def test_tests_run_against_the_deployed_dependency_set(package):
    """The deployed app installs app/uv.lock with `uv run --frozen`.

    If the root dev environment resolved a different version, these tests would
    be green against code that never ships. Fix with `uv lock` in both projects.
    """
    locked = {p["name"]: p["version"] for p in tomllib.loads(APP_LOCK.read_text())["package"]}
    assert metadata.version(package) == locked[package]


def test_app_has_no_requirements_txt():
    """Databricks Apps uses pip, and ignores uv.lock, whenever requirements.txt exists.

    Adding one would silently swap the locked FastMCP for whatever pip resolves.
    """
    assert not (APP_LOCK.parent / "requirements.txt").exists()
