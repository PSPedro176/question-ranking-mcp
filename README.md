# hello-mcp

A minimal hello-world MCP server registered in **Unity Catalog** and deployed 100% with a
Declarative Automation Bundle (CLI >= 1.23.0). Default stack: `dev_ai.hello_mcp.hello`.

## Architecture

```
schema dev_ai.hello_mcp
 ├─ app  mcp-hello                 FastMCP, tool `say_hello`, streamable HTTP at /mcp (stateless)
 ├─ UC HTTP connection hello_mcp_conn   OAuth M2M as the app's own service principal
 │     created by job `create_connection` (bundle job_run, fires on every deploy, idempotent)
 └─ UC MCP service hello           -> connection; EXECUTE granted to `account users`
```

Everything that has a bundle resource type is declared in `resources/`: schema, app (+ `CAN_USE`
for its own service principal), job, job_run, `mcp_services` and its grants. UC connections have
no bundle resource type, so `jobs/create_connection.py` creates it during `bundle deploy`;
`mcp_service` declares `lifecycle.depends_on` on that job run.

## Deploy

```bash
make deploy PROFILE=accelerate-gateway   # bundle deploy + bundle run hello_app (ships app code)
make smoke  PROFILE=accelerate-gateway   # initialize / tools/list / tools/call through the UC gateway
make test lint                           # pytest + ruff (uses uv)
```

`catalog` (default `dev_ai`) and `schema` (default `hello_mcp`) are bundle variables, e.g.
`databricks bundle deploy -t dev --var schema=my_schema`. The `dev` target is pinned to
`https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com`. Re-running `make deploy` is safe.
Use `make deploy FQN=...` / `make smoke FQN=<catalog>.<schema>.hello` when overriding variables.

## Referencing the MCP in the Unity Gateway coding-agent config

This repo does not touch that config. Add the UC name and/or gateway URL:

- names: `["dev_ai.hello_mcp.hello"]` (`<catalog>.<schema>.<service>`)
- URL: `https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com/ai-gateway/mcp-services/dev_ai.hello_mcp.hello`

Callers need `EXECUTE` on the MCP service (granted to `account users` via `var.mcp_consumers`),
plus `USE CATALOG`/`USE SCHEMA`.

## Notes and caveats

- **Auth identity.** The job mints one OAuth secret for the app's service principal and passes it
  straight to the connection; it is never logged, committed or stored elsewhere. The secret
  expires after 2 years. To rotate, delete the connection (`databricks connections delete
  dev_ai.hello_mcp.hello_mcp_conn`) and run `make deploy` (not exercised; UC may require removing the MCP service first, e.g. with `bundle destroy`).
- If the app is deleted and recreated, its service principal changes: the connection must be recreated
  the same way.
- The bundle does not use `mode: development` so the schema keeps its exact name.
- `app/requirements.txt` (pip) is used rather than a `uv.lock`: a lock created on a machine with
  an internal PyPI proxy embeds proxy URLs the Apps build cannot reach.
- `bundle deploy` alone does not ship app code changes; `make deploy` runs `bundle run hello_app`.
