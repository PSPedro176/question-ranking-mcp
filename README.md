# hello-mcp

A minimal hello-world MCP server registered in **Unity Catalog** and deployed 100% with a
Declarative Automation Bundle (CLI >= 1.23.0). Two targets in one workspace:
`dev` -> `dev_ai.hello_mcp.hello` (app `mcp-hello`) and `prod` -> `prod_ai.hello_mcp.hello` (app `mcp-hello-prod`).

## Architecture

```
schema <catalog>.hello_mcp            (dev_ai for `dev`, prod_ai for `prod`)
 ├─ app  mcp-hello / mcp-hello-prod  FastMCP, tool `say_hello`, streamable HTTP at /mcp (stateless)
 ├─ UC HTTP connection hello_mcp_conn   OAuth M2M as the app's own service principal
 │     created by job `create_connection` (bundle job_run, fires on every deploy, idempotent)
 └─ UC MCP service hello           -> connection (no grants beyond the owner unless configured, see Access)
```

Everything that has a bundle resource type is declared in `resources/`: schema, app (+ `CAN_USE`
for its own service principal), job, job_run, `mcp_services` and its grants. UC connections have
no bundle resource type, so `jobs/create_connection.py` creates it during `bundle deploy`;
`mcp_service` declares `lifecycle.depends_on` on that job run.

## Deploy with the Databricks CLI only

Run from the repo root. Replace `dev` with `prod` (catalog `prod_ai`, app `mcp-hello-prod`) for
production; the target carries the catalog and app name, so no `--var` is needed. Commands marked
**[verified]** were run against the live `dev` stack; `prod` commands are only **[validated]**
(`bundle validate -t prod` passes; nothing has been deployed to `prod_ai`).

```bash
# 1. Authenticate once (interactive, opens a browser)          [not run here; the profile already existed]
databricks auth login --host https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com --profile accelerate-gateway

# 2. Validate                                                  [verified: dev] [validated: prod]
databricks bundle validate -t dev --profile accelerate-gateway
databricks bundle validate -t prod --profile accelerate-gateway

# 3. Preview what a deploy would change (read-only)            [verified: dev]
databricks bundle plan -t dev --profile accelerate-gateway

# 4. Deploy: schema, app, connection job (runs during deploy), MCP service   [verified: dev]
databricks bundle deploy -t dev --profile accelerate-gateway
databricks bundle deploy -t prod --profile accelerate-gateway

# 5. Ship the app code and start it (deploy alone does not)    [verified: dev]
databricks bundle run hello_app -t dev --profile accelerate-gateway
databricks bundle run hello_app -t prod --profile accelerate-gateway

# 6. See the resources and their URLs                          [verified: dev]
databricks bundle summary -t dev --profile accelerate-gateway
databricks apps get mcp-hello --profile accelerate-gateway          # app URL (mcp-hello-prod for prod)
```

Verify the MCP through the Unity Catalog gateway (initialize, tools/list, tools/call):

```bash
databricks ai-gateway get-mcp-service mcp-services/dev_ai.hello_mcp.hello --profile accelerate-gateway   # [verified]
uv run python scripts/smoke_test.py --profile accelerate-gateway --name dev_ai.hello_mcp.hello          # [verified]
uv run python scripts/smoke_test.py --profile accelerate-gateway --name prod_ai.hello_mcp.hello         # [not run]
```

**Overriding variables.** `catalog` and `app_name` come from the target; the rest default to
`schema=hello_mcp`, `mcp_service_id=hello`, `connection_name=hello_mcp_conn`. Override any scalar
per command with `--var`, e.g. `databricks bundle deploy -t dev --var schema=my_schema --profile ...`,
and pass the same `--var`s to `bundle run`, `bundle destroy` and the smoke-test name. The complex
grants variables (`mcp_service_grants`, `schema_grants`) cannot be set with `--var`; set them under
`targets.<target>.variables` in `databricks.yml` (commented examples are there).

**Tear down** (order matters: the connection is created by a job, not by the bundle, so
`bundle destroy` cannot remove it, and a schema that still contains it cannot be dropped; the MCP
service that references it goes first). **[not run against the live stacks]**; the same sequence via `make destroy` was
verified end to end on a scratch schema/app:

```bash
databricks ai-gateway delete-mcp-service mcp-services/dev_ai.hello_mcp.hello --profile accelerate-gateway
databricks connections delete dev_ai.hello_mcp.hello_mcp_conn --profile accelerate-gateway
databricks bundle destroy -t dev --profile accelerate-gateway
# prod: mcp-services/prod_ai.hello_mcp.hello, prod_ai.hello_mcp.hello_mcp_conn, -t prod
```

(`scripts/teardown.py` does the first two idempotently, ignoring only NOT_FOUND.)

### DABs concepts illustrated

- **Targets** (`dev`, `prod`) pick the host and per-target **variables** (catalog, app name); bundle
  state is per target under `~/.bundle/hello-mcp/<target>`, so the targets never collide. App names
  are workspace-global, hence two names.
- **Resources** are declared in `resources/` and cross-referenced (`${resources.apps.hello_app.name}`),
  which gives deploy ordering; `lifecycle.depends_on` adds ordering where there is no reference
  (the MCP service waits for the connection job run).
- **`job_runs` with `on_bundle_deploy`** runs the (idempotent) job on every `bundle deploy`: the
  escape hatch for things with no resource type, here the UC connection.
- **`bundle run`** executes a runnable resource; for an app it deploys the synced source and starts it.

## Makefile (convenience wrapper)

The Makefile only wraps the commands above. `PROFILE` is required; `TARGET` (`dev` default, or
`prod`) selects the target, and catalog/schema/service/connection names are read from the target's
variables. `CATALOG`, `SCHEMA`, `SERVICE`, `CONNECTION`, `APP` are optional explicit overrides.

```bash
make deploy  PROFILE=<profile> [TARGET=prod]   # bundle deploy + bundle run hello_app
make smoke   PROFILE=<profile> [TARGET=prod]   # gateway initialize / tools/list / tools/call
make destroy PROFILE=<profile> [TARGET=prod]   # delete MCP service + connection, then bundle destroy
make test lint                                 # pytest + ruff (uses uv)
```

## Referencing the MCP in the Unity Gateway coding-agent config

This repo does not touch that config. Add the UC name and/or gateway URL:

- names: `["dev_ai.hello_mcp.hello"]` or `["prod_ai.hello_mcp.hello"]` (`<catalog>.<schema>.<service>`)
- URL: `https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com/ai-gateway/mcp-services/<catalog>.hello_mcp.hello`

## Access

Only the owner (the deploying identity) can use the MCP by default. To share it, set both
grant lists under `targets.<target>.variables` in `databricks.yml` (complex variables cannot be
passed with `--var`; a commented example is there): `mcp_service_grants` (`EXECUTE`) and
`schema_grants` (`USE_SCHEMA`). `USE CATALOG` on the catalog is **not** managed by this bundle
(the catalog pre-exists), so those principals must already hold it.

Grants are not revoked by other deployments: with an empty list, a principal granted earlier
(e.g. by a previous version of this bundle, or by hand) keeps its grant on a stack deployed
elsewhere. Revoke manually, e.g.
`databricks grants update mcp_service <catalog>.<schema>.<service> --json '{"changes":[{"principal":"account users","remove":["EXECUTE"]}]}'`.
Access can also be inherited from catalog-level grants (on `dev_ai`, `account users` has
`ALL_PRIVILEGES` on the catalog; on `prod_ai` only `USE_CATALOG`), which this bundle does not control.

## Notes and caveats

- **Auth identity.** The job mints an OAuth secret for the app's service principal only when
  the connection is missing or stale (host, base path, client id or token endpoint differ from the
  app as it is now), and passes it straight to the connection. It is revoked if creating or
  updating the connection fails, and never logged. Re-deploys with nothing stale mint nothing. A
  stale connection (for example after the app was recreated, which changes its service principal)
  is repaired in place with `connections.update`; this works while the MCP service references it.
  Secrets expire after 2 years; to rotate, delete the connection and redeploy. Old secrets of a
  replaced service principal cannot be revoked by the job (their ids are unknown).
- `make destroy` follows the tear-down order above (`scripts/teardown.py` ignores only a confirmed
  NOT_FOUND, other errors fail the target).
- The bundle does not use `mode: development` so the schema keeps its exact name.
- `app/requirements.txt` (pip) is used rather than a `uv.lock`: a lock created on a machine with
  an internal PyPI proxy embeds proxy URLs the Apps build cannot reach.
- `bundle deploy` alone does not ship app code changes; run `bundle run hello_app` after it.
