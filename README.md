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

`PROFILE` (a Databricks CLI profile) is required; the Makefile refuses to run without it.

```bash
make deploy PROFILE=<profile>    # bundle deploy + bundle run hello_app (ships app code)
make smoke  PROFILE=<profile>    # initialize / tools/list / tools/call through the UC gateway
make test lint                   # pytest + ruff (uses uv)
make destroy PROFILE=<profile>   # bundle destroy, then deletes the UC connection (idempotent)
```

`CATALOG` (default `dev_ai`), `SCHEMA` (`hello_mcp`), `SERVICE` (`hello`) and `CONNECTION`
(`hello_mcp_conn`) are passed to every bundle command and to the smoke test, e.g.
`make deploy PROFILE=p SCHEMA=my_schema`. The `dev` target is pinned to
`https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com`. Re-running `make deploy` is safe.

## Referencing the MCP in the Unity Gateway coding-agent config

This repo does not touch that config. Add the UC name and/or gateway URL:

- names: `["dev_ai.hello_mcp.hello"]` (`<catalog>.<schema>.<service>`)
- URL: `https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com/ai-gateway/mcp-services/dev_ai.hello_mcp.hello`

## Access

Only the owner (the deploying identity) can use the MCP by default. To share it, set both
grant lists under `targets.<target>.variables` in `databricks.yml` (complex variables cannot be
passed with `--var`; a commented example is there): `mcp_service_grants` (`EXECUTE`) and
`schema_grants` (`USE_SCHEMA`). `USE CATALOG` on the catalog is **not** managed by this bundle
(the catalog pre-exists), so those principals must already hold it.

## Notes and caveats

- **Auth identity.** The job mints an OAuth secret for the app's service principal only when
  the connection is missing or stale (host, base path, client id or token endpoint differ from the
  app as it is now), and passes it straight to the connection. It is revoked if creating or
  updating the connection fails, and never logged. Re-deploys with nothing stale mint nothing. A
  stale connection (for example after the app was recreated, which changes its service principal)
  is repaired in place with `connections.update`; this works while the MCP service references it.
  Secrets expire after 2 years; to rotate, delete the connection and redeploy. Old secrets of a
  replaced service principal cannot be revoked by the job (their ids are unknown).
- `make destroy` runs `bundle destroy` and then deletes the connection (created outside the
  bundle). The destroy path has not been exercised against the live demo stack.
- The bundle does not use `mode: development` so the schema keeps its exact name.
- `app/requirements.txt` (pip) is used rather than a `uv.lock`: a lock created on a machine with
  an internal PyPI proxy embeds proxy URLs the Apps build cannot reach.
- `bundle deploy` alone does not ship app code changes; `make deploy` runs `bundle run hello_app`.
