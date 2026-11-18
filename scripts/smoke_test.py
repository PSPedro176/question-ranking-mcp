"""End-to-end check through the Unity Catalog AI Gateway: initialize, tools/list, tools/call.

Usage: uv run python scripts/smoke_test.py --profile <cli-profile> [--name dev_ai.hello_mcp.hello]

The bearer token comes from `databricks auth token` and is never printed.
"""

import argparse
import json
import subprocess
import sys

import httpx


def parse_response(resp: httpx.Response) -> dict:
    """Decode a JSON or SSE (`data: ...`) MCP response body."""
    resp.raise_for_status()
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise RuntimeError("SSE response carried no data event")
    return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--name", default="dev_ai.hello_mcp.hello", help="<catalog>.<schema>.<service>")
    ap.add_argument("--who", default="smoke-test")
    args = ap.parse_args()

    auth = json.loads(
        subprocess.check_output(["databricks", "auth", "token", "--profile", args.profile])
    )
    describe = subprocess.check_output(
        ["databricks", "auth", "describe", "--profile", args.profile, "-o", "json"]
    )
    host = json.loads(describe)["details"]["host"].rstrip("/")
    url = f"{host}/ai-gateway/mcp-services/{args.name}"
    headers = {
        "Authorization": f"Bearer {auth['access_token']}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    print(f"POST {url}")

    def rpc(client: httpx.Client, id_: int | None, method: str, params: dict | None = None):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if id_ is not None:
            body["id"] = id_
        return client.post(url, headers=headers, json=body)

    with httpx.Client(timeout=60) as client:
        init = parse_response(
            rpc(
                client,
                1,
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "0"},
                },
            )
        )
        print("initialize     ->", init["result"]["serverInfo"])
        rpc(client, None, "notifications/initialized")

        tools = parse_response(rpc(client, 2, "tools/list"))["result"]["tools"]
        names = [t["name"] for t in tools]
        print("tools/list     ->", names)
        if "say_hello" not in names:
            print("FAIL: say_hello not listed")
            return 1

        call = parse_response(
            rpc(client, 3, "tools/call", {"name": "say_hello", "arguments": {"name": args.who}})
        )["result"]
        text = call["content"][0]["text"]
        print("tools/call     ->", text)
        if call.get("isError") or text != f"Hello, {args.who}!":
            print("FAIL: unexpected tool result")
            return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
