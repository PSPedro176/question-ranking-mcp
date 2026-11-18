# question-ranking-mcp

A custom **MCP server on Databricks Apps** that ranks questions by quality and
relevance, scoring them with a **Model Serving endpoint that splits traffic 50/50 across
two open-source models**. Everything deploys from a single Declarative Automation Bundle.

Splitting the traffic is the point: question quality is subjective, and routing half the
scoring calls to each of two different open-source models keeps the rankings from
inheriting one model's particular bias.

## Architecture

```
MCP client (AI Playground, Claude Code, Cursor, agent)
        │  streamable HTTP
        ▼
┌─────────────────────────────────────────────┐
│ 1. Databricks App: mcp-question-ranking     │   resources/app.yml
│    FastAPI + FastMCP, MCP at /mcp           │
│    tools: rank_questions, best_question,    │
│           whoami                            │
└───────────────────┬─────────────────────────┘
                    │  CAN_QUERY as the app's own service principal
                    ▼
┌─────────────────────────────────────────────┐
│ 2. Serving endpoint: question-ranking-router│   resources/serving.yml
│    50% system.ai.llama_v3_2_3b_instruct     │
│    50% system.ai.mistral_7b_instruct_v0_2   │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ 3. Unity Catalog: prod_ai.                  │   resources/uc.yml
│    `question-ranking-mcp` schema            │
│    + opt-in MCP registration job            │
└─────────────────────────────────────────────┘
```

| Path | What it is |
|---|---|
| `databricks.yml` | Bundle root: variables and the `dev` / `prod` targets |
| `resources/app.yml` | The MCP server on Databricks Apps |
| `resources/serving.yml` | The 50/50 traffic-split endpoint |
| `resources/uc.yml` | UC schema + the opt-in registration job |
| `app/` | App source, including its own `app.yaml` |
| `app/server/ranking.py` | All ranking logic — pure, no I/O, fully unit-tested |
| `jobs/register_uc_mcp.py` | Idempotent UC connection + MCP service registration |
| `app/uv.lock` | The exact dependency set the app deploys with (`uv run --frozen`) |
| `tests/` | 112 tests (pure logic + real-protocol integration), no network or auth |

## The tools

| Tool | Signature | Returns |
|---|---|---|
| `rank_questions` | `(questions: list[str], context: str \| None)` | All questions sorted best-first with `rank`, `score` (0–10) and `rationale` |
| `best_question` | `(questions: list[str], context: str \| None)` | Just the strongest question, plus the runner-up |
| `whoami` | `()` | Calling user and the configured endpoint; doubles as a health check |

`context` is optional. Supply it and scores become relevance-aware; omit it and they
measure question quality alone. Input is capped at 25 questions of 500 characters each.

Scoring is deliberately failure-tolerant. The two small instruct models behind the
endpoint do not reliably emit clean JSON, so the parser accepts bare JSON, fenced blocks
and JSON wrapped in prose; keys rows back to the input by the model's `index` rather than
trusting its ordering; and falls back to a neutral 5.0 for any question the model never
scored. `rank_questions` reports `model_scored` so a caller can see how much of the
ranking the model actually produced.

## Why these two models

The 50/50 split constrains the choice more than it first appears:

- **Pay-per-token Foundation Model endpoints cannot be traffic-split.** There is no
  pay-per-token served-entity form, so the split has to live on an endpoint you own.
- **One endpoint cannot mix model types.** Both entities must be provisioned throughput.
- **Both entities must share a scaling family.** These two are throughput-band scaled
  (`min_provisioned_throughput` / `max_provisioned_throughput`), so they coexist.

| | Model | Version | `max_provisioned_throughput` |
|---|---|---|---|
| A | `system.ai.llama_v3_2_3b_instruct` | `2` | 22000 (one chunk) |
| B | `system.ai.mistral_7b_instruct_v0_2` | `2` | 2450 (one chunk) |

Both are genuinely open-source and small. `min_provisioned_throughput: 0` lets each scale
to zero, and each maximum is exactly **one** throughput chunk — the cheapest valid
configuration. `max_provisioned_throughput` must be a multiple of the model's
`throughput_chunk_size`, which differs per model, hence the different numbers.

> **This model inventory was read from a fallback workspace, not the deploy target.**
> Every model field is a bundle variable for that reason. If the target workspace offers a
> different set, override rather than edit:
>
> ```bash
> databricks bundle deploy -t prod \
>   --var model_a_name=system.ai.<model> --var model_a_version=1 --var model_a_max_throughput=<chunk> \
>   --var model_b_name=system.ai.<model> --var model_b_version=1 --var model_b_max_throughput=<chunk>
> ```
>
> Read the valid chunk size from the model's PT serving metadata before overriding; a
> throughput that is not a multiple of the chunk size is rejected at deploy.

## Prerequisites

- Databricks CLI **v1.23.0 or newer** (`databricks --version`).
- Databricks Apps enabled in the workspace, in a region that supports Model Serving.
- Unity Catalog, with the **`prod_ai` catalog already existing**. The bundle creates only
  the schema. Creating a catalog is not reliably idempotent across workspaces and would
  need metastore-level rights, so it is deliberately out of scope — override
  `--var catalog=<name>` to point at a different existing catalog.
- `uv` for local development. The repo pins Python 3.12 (`.python-version`); the Apps
  runtime itself is Python 3.11, which `app/pyproject.toml` targets.

### The `agents-deploy` service principal

The intended `prod` deploy identity is a service principal named **`agents-deploy`**,
which **does not exist yet**. Create it, then grant exactly:

| Scope | Grant | Why |
|---|---|---|
| Workspace | **Workspace access** entitlement | Required to deploy at all; a Consumer-access principal cannot |
| Workspace | Can use **Databricks Apps** | To create the app |
| Catalog `prod_ai` | `USE CATALOG`, `CREATE SCHEMA` | To create the `question-ranking-mcp` schema |
| Both served models | `USE CATALOG` + `USE SCHEMA` on `system.ai`, `EXECUTE` on each model | To serve them from the endpoint |

**Unity Catalog SQL identifies a service principal by its application ID, not its
display name.** ``TO `agents-deploy` `` would not grant the service principal — it would
look for a user or group of that name. Look the ID up first:

```bash
databricks service-principals list --filter 'displayName eq "agents-deploy"' \
  --profile <prod-profile> | jq -r '.[].applicationId'
```

Then substitute it for `<agents-deploy-application-id>` below:

```sql
GRANT USE CATALOG, CREATE SCHEMA ON CATALOG prod_ai TO `<agents-deploy-application-id>`;

GRANT USE CATALOG ON CATALOG system TO `<agents-deploy-application-id>`;
GRANT USE SCHEMA  ON SCHEMA  system.ai TO `<agents-deploy-application-id>`;
GRANT EXECUTE ON MODEL system.ai.llama_v3_2_3b_instruct  TO `<agents-deploy-application-id>`;
GRANT EXECUTE ON MODEL system.ai.mistral_7b_instruct_v0_2 TO `<agents-deploy-application-id>`;
```

Only if you also opt into UC MCP registration (see below):

```sql
GRANT CREATE CONNECTION, CREATE SERVICE, USE SCHEMA
  ON SCHEMA prod_ai.`question-ranking-mcp` TO `<agents-deploy-application-id>`;

-- Only if you also pass a bearer token via mcp_bearer_token_secret:
GRANT READ_SECRET ON SECRET <catalog>.<schema>.<secret> TO `<agents-deploy-application-id>`;
```

Note the backticks. Hyphens are legal in a UC schema name, and the bundle YAML takes the
literal `question-ranking-mcp`, but **SQL requires backtick-quoting**:
``prod_ai.`question-ranking-mcp` ``.

> **`run_as` does not apply here.** Bundle `run_as` only affects jobs and pipelines.
> Serving endpoints and Apps are owned by whichever identity ran `databricks bundle
> deploy`, and the App additionally gets its *own* runtime service principal, distinct
> from `agents-deploy`. That runtime principal is what receives `CAN_QUERY` on the
> endpoint via the app's `resources` block — you do not grant it by hand.

## Deploy

```bash
# Validate first - always.
databricks bundle validate -t dev  --profile <your-profile>
databricks bundle validate -t prod --profile <prod-profile>

# Deploy the bundle, then deploy and start the app's code.
databricks bundle deploy -t prod --profile <prod-profile>
databricks bundle run question_ranking_mcp -t prod --profile <prod-profile>

# The same for dev.
databricks bundle deploy -t dev --profile <your-profile>
databricks bundle run question_ranking_mcp -t dev --profile <your-profile>

# Find the app URL.
databricks apps get mcp-question-ranking --profile <prod-profile> | jq -r .url
```

**Always follow `bundle deploy` with `bundle run question_ranking_mcp`** — that runs the
app resource by its bundle key. `bundle deploy` uploads the source but does not reliably
ship it to the app:

- On a **fresh install**, `lifecycle.started: true` makes `bundle deploy` start the app and
  create its first deployment from the synced source.
- On a **later deploy that only changes app code**, `bundle deploy` uploads the new files
  but does not redeploy the app. The CLI only re-runs the app's lifecycle step when an app
  *config* field changes, so the app keeps serving the previous code.

`bundle run` on an app starts its compute if stopped and then always creates a new
deployment from the bundle's synced source. Do not use `databricks apps start` for this: it
starts compute and resumes the *last* deployment, so it can bring stale code back up.

### Choosing a workspace

`dev` has **no pinned host**: it deploys to whichever workspace `--profile` selects, so
any developer can use it with their own profile.

`prod` is **pinned** to `https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com`.
The CLI **rejects** a `--profile` whose host differs from a target's explicit
`workspace.host` — a profile cannot redirect `prod`. To deploy `prod` elsewhere, change
`targets.prod.workspace.host` in `databricks.yml`.

### App names in `dev`

`mode: development` prefixes most resource names per user but **not** app names, because
app names are restricted to lowercase letters, digits and hyphens with a 30-character
limit — and `bundle validate` enforces neither rule. The `dev` target therefore builds the
name itself as `mcp-qrank-${workspace.current_user.domain_friendly_name}` (for example
`mcp-qrank-pedro-perdomo`, 23 characters). It uses `domain_friendly_name` rather than
`short_name` because `short_name` keeps underscores (`pedro_perdomo`), which are illegal
in an app name.

That leaves 20 characters for the suffix. If your sanitised username is longer, override it:

```bash
databricks bundle deploy -t dev --profile <your-profile> --var dev_app_suffix=<short>
databricks bundle run question_ranking_mcp -t dev --profile <your-profile> --var dev_app_suffix=<short>

# or replace the whole name, which takes precedence:
databricks bundle deploy -t dev --profile <your-profile> --var app_name=mcp-<something>
```

Pass the **same** `--var` to `bundle run`. It resolves the app name from the same
variable, so omitting it would target a different app.

The endpoint provisions GPU capacity and can take several minutes to go green:

```bash
databricks serving-endpoints get question-ranking-router --profile <prod-profile> \
  | jq '{ready: .state.ready, config_update: .state.config_update}'
# usable once ready == READY and config_update == NOT_UPDATING
```

## Connect an MCP client

The server speaks **streamable HTTP** at `/mcp`:

```
https://<app-url>/mcp
```

`stateless_http=True` is set deliberately: several Databricks MCP clients (the AI
Playground and the Assistant among them) do not send an `mcp-session-id` header, and a
stateful server rejects their follow-up requests with HTTP 400.

In the **AI Playground**, the app appears in the MCP server picker — this is why the app
name carries the `mcp-` prefix, which the Playground uses to recognise it. The Playground
authenticates you itself.

### Authenticating an external client

The endpoint is protected: every request needs a Databricks **OAuth** access token in the
`Authorization` header. **Personal access tokens are not supported** for MCP callers.

**1. Get access to the app.** The caller needs the **CAN USE** permission on the app. The
deploying identity already has CAN MANAGE; grant everyone else explicitly:

```bash
databricks apps update-permissions mcp-question-ranking --profile <prod-profile> --json '{
  "access_control_list": [{"user_name": "someone@example.com", "permission_level": "CAN_USE"}]
}'
```

**2. Sign in once with OAuth (U2M).** This opens a browser and caches a refresh token:

```bash
databricks auth login --host https://<workspace-host> --profile mcp-user
```

**3. Get an access token and check it works.** A CLI-issued token carries the `all-apis`
scope, which covers the app's user authorization scopes:

```bash
export DATABRICKS_TOKEN=$(databricks auth token --profile mcp-user | jq -r .access_token)

curl -s https://<app-url>/mcp \
  -H "Authorization: Bearer $DATABRICKS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
```

A successful reply names you under `user`, which confirms both the token and the
on-behalf-of-user forwarding.

**4. Configure your client** to send the same header. For Claude Code (`.mcp.json`, which
expands `${VAR}` from the environment):

```json
{
  "mcpServers": {
    "question-ranking": {
      "type": "http",
      "url": "https://<app-url>/mcp",
      "headers": { "Authorization": "Bearer ${DATABRICKS_TOKEN}" }
    }
  }
}
```

**Tokens expire after about an hour** — the `expiry` field in the `databricks auth token`
output gives the exact time. The header above is fixed when the client starts, so on
expiry re-run step 3 (the CLI refreshes the token silently from the cached refresh token)
and restart the client. Re-run step 2 only if the refresh token itself has lapsed.

### Which identity does what

The app declares the `iam.current-user:read` user API scope, so Apps forwards the caller's
token in `x-forwarded-access-token`; the `whoami` tool reads it. The `rank_questions` and
`best_question` tools instead call the serving endpoint as the app's *own* service
principal, so ranking works for any caller with CAN USE, without each user needing access
to the endpoint.

## Unity Catalog registration

The bundle creates the schema `prod_ai.question-ranking-mcp` declaratively. Registering
the MCP server itself as a UC **MCP service** is **opt-in and expected to fail**.

### The limitation

Databricks' own documentation
([External MCP servers](https://docs.databricks.com/aws/en/ai-gateway/register-mcp-service),
*Limitations*) states:

> You can register external MCP servers. Registering Genie, **Databricks Apps**, or Unity
> Catalog entity sources as your own MCP isn't supported. Use a Databricks-provided MCP or
> connect directly to your server on Databricks Apps.

It also notes that **SQL commands such as `CREATE MCP SERVICE` aren't supported**, and
that **UC Global Search does not surface MCPs** — find them on the MCPs page.

An Apps-hosted MCP server is exactly the source that is excluded. Databricks does not
document *why*, and this README does not guess.

**The supported consumption path is connecting your MCP client directly to
`https://<app-url>/mcp`**, as described above. That path works today and needs no UC
registration.

### How it is implemented, and why as a job

Registration lives in a **bundle job** (`jobs/register_uc_mcp.py`), not a declarative
`resources.mcp_services` block. Two reasons:

1. A UC MCP service requires a pre-existing **schema-level HTTP connection**, and there is
   **no `connections` DABs resource type** — verified against `databricks bundle schema`.
   A declarative `mcp_services` block would therefore fail on a clean deploy, because
   nothing in the bundle can create the connection first.
2. Because the platform rejects this source, a declarative resource would fail **every**
   `databricks bundle deploy`. As a job, a rejection fails one job run and leaves the
   three working components deployed.

The job is **idempotent** (it reuses an existing connection or MCP service) and defaults
to a **dry run**, so `databricks bundle deploy` always succeeds. To attempt it:

```bash
databricks bundle deploy -t prod --profile <prod-profile> \
  --var register_uc_mcp_service=true \
  --var mcp_bearer_token_secret=<catalog>.<schema>.<secret>

databricks bundle run register_uc_mcp -t prod --profile <prod-profile>
```

With `register_uc_mcp_service=false` (the default) the job prints the plan it *would*
execute and exits cleanly. With it set to `true` it resolves the deployed app's URL,
creates the schema-level HTTP connection, then calls `create-mcp-service`. **If Unity
Catalog rejects that call, the job fails with a message pointing back at this section.**
That is the documented limitation, not a defect in the bundle.

### The bearer token must be a Unity Catalog Secret

`mcp_bearer_token_secret` takes a **Unity Catalog Secret** as a three-level fully
qualified name, `<catalog>.<schema>.<secret>` — the form that
`connections.create(secrets=...)` documents ("UC Secret fully qualified names") and that
`secrets_uc.get_secret` accepts.

It is **not** a workspace secret scope. A `<scope>/<key>` path, with or without a
`secrets/` prefix, is a different and incompatible namespace; the job validates the format
up front and rejects it with an explicit message rather than letting it fail opaquely
inside the connections API. The job also preflights the secret with
`secrets_uc.get_secret` so a missing secret or a missing `READ_SECRET` grant produces a
clear error instead of a confusing connection failure.

> One ambiguity worth flagging, since it is unverified against a live workspace: a sibling
> API (model provider services) documents its secret references in the **resource-name**
> form `secrets/{catalog}.{schema}.{secret}`, while the connections `secrets` map is
> documented as a bare fully qualified name. This bundle follows the connections API's own
> wording. If a real registration attempt ever gets far enough to reject the secret
> reference specifically, try the `secrets/`-prefixed form.

If a future release does support it, the MCP would be consumed as:

```
https://<workspace-hostname>/ai-gateway/mcp-services/prod_ai.question-ranking-mcp.question_ranking
```

## Development

```bash
uv sync

uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

There are two test files, and neither needs network access or Databricks auth:

- **`tests/test_ranking.py`** tests the pure logic in `app/server/ranking.py`: input
  validation, score and index coercion, JSON extraction and salvage, the index policy that
  keeps every score on the question it was given for, fallbacks, ordering, and prompt
  construction.
- **`tests/test_server.py`** drives the real FastAPI + FastMCP app over the
  streamable-HTTP MCP protocol, with the LLM and `WorkspaceClient` stubbed. It covers MCP
  initialization followed by a request with no session id, `/health` inside the same
  lifespan, both ranking tools on success and on failure, and overlapping requests that
  carry distinct forwarded tokens. These boundaries are where bugs hide that the pure
  tests cannot see: `best_question` once broke on a FastMCP version that turned decorated
  tools into non-callable objects, and no pure test noticed.

The deployed app runs `uv run --frozen` against `app/uv.lock`. A drift test fails if the
dev environment resolves different versions of the key packages, so the tests always run
against the dependency set that actually ships. After changing dependencies, refresh both
locks:

```bash
uv lock --project app && uv lock && uv sync
```

Run the server locally:

```bash
cd app
SERVING_ENDPOINT=question-ranking-router uv run question-ranking-mcp
curl localhost:8000/health
```

It binds `0.0.0.0` on `DATABRICKS_APP_PORT` (**not** `PORT`), defaulting to 8000.

> `app/app.yaml` uses camelCase **`valueFrom`**, while the DABs equivalent in
> `resources/app.yml` uses snake_case **`value_from`**. Both files are real and the casing
> genuinely differs — a common source of silent misconfiguration.

## License

[Apache-2.0](LICENSE).
