# question-ranking-mcp

A custom **MCP server on Databricks Apps** that ranks questions by quality and
relevance, scoring them with a **Unity Gateway model service that splits traffic 50/50
across two open-source models**. Everything deploys from a single Declarative Automation
Bundle.

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
                    │  EXECUTE as the app's own service principal
                    ▼
┌─────────────────────────────────────────────┐
│ 2. Model service: question_ranking_router   │   resources/model_service.yml
│    in prod_ai.`question-ranking-mcp`        │
│    50% system.ai.gpt-oss-20b                │
│    50% system.ai.qwen3-next-80b-a3b-instruct│
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
| `resources/model_service.yml` | The 50/50 model service |
| `resources/uc.yml` | UC schema + the opt-in registration job |
| `app/` | App source, including its own `app.yaml` |
| `app/server/ranking.py` | All ranking logic — pure, no I/O, fully unit-tested |
| `jobs/register_uc_mcp.py` | Idempotent UC connection + MCP service registration |
| `app/uv.lock` | The exact dependency set the app deploys with (installed by `uv sync`) |
| `tests/` | Pure-logic and real-protocol integration tests, no network or auth |

## The tools

| Tool | Signature | Returns |
|---|---|---|
| `rank_questions` | `(questions: list[str], context: str \| None)` | All questions sorted best-first with `rank`, `score` (0–10) and `rationale` |
| `best_question` | `(questions: list[str], context: str \| None)` | Just the strongest question, plus the runner-up |
| `whoami` | `()` | Calling user and the configured model service; doubles as a health check |

`context` is optional. Supply it and scores become relevance-aware; omit it and they
measure question quality alone. Input is capped at 25 questions of 500 characters each.

Scoring is deliberately failure-tolerant. The two models behind the model service do not
reliably emit clean JSON, so the parser accepts bare JSON, fenced blocks
and JSON wrapped in prose; keys rows back to the input by the model's `index` rather than
trusting its ordering; and falls back to a neutral 5.0 for any question the model never
scored. `rank_questions` reports `model_scored` so a caller can see how much of the
ranking the model actually produced.

## The model service and its two models

The 50/50 split lives in a **Unity Gateway model service**: a Unity Catalog securable that
routes each request to one of its destinations by `traffic_percentage`. It is defined in
`resources/model_service.yml`, in `prod_ai`.`question-ranking-mcp`. Its destinations are
**pay-per-token** Databricks foundation models, so there is no endpoint to provision,
size or keep warm, and no idle cost.

| | Model (Unity Catalog name) | License | Share |
|---|---|---|---|
| A | `system.ai.gpt-oss-20b` | Apache-2.0 | 50% |
| B | `system.ai.qwen3-next-80b-a3b-instruct` | Apache-2.0 | 50% |

Both are genuinely open-source (Apache-2.0), not only open-weights. Both are also cheap to
run: `gpt-oss-20b` is a 21B model, and Qwen3-Next is a mixture-of-experts model with only
about 3B parameters active per token. `meta-llama-3-1-8b-instruct` and `gemma-3-12b` are
smaller still, but ship under the Llama Community License and Gemma Terms of Use, which
restrict use.

The two models are bundle variables. Give the **Unity Catalog** model name, which can
differ from the serving endpoint name; for example, the endpoint
`databricks-meta-llama-3-1-8b-instruct` is the model `system.ai.meta_llama_v3_1_8b_instruct`:

```bash
databricks bundle deploy -t prod --profile <prod-profile> \
  --var model_a=system.ai.<model> --var model_b=system.ai.<model>
```

The app reaches the service through Unity Gateway's OpenAI-compatible Chat Completions
API, `POST /ai-gateway/mlflow/v1/chat/completions`, passing the service's full name,
`<catalog>.<schema>.<id>`, as `model`.

## Prerequisites

- Databricks CLI **v1.23.0 or newer** (`databricks --version`).
- Databricks Apps enabled in the workspace, in a region that supports Unity Gateway.
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
| Catalog `prod_ai` | `USE CATALOG`, `CREATE SCHEMA` | To create the `question-ranking-mcp` schema. As its owner, the principal can then create the model service in it (`CREATE SERVICE`) |
| Both models | `USE CATALOG` + `USE SCHEMA` on `system.ai`, `EXECUTE` on each model | To create the model service, and to answer its queries. Model services use the **owner's** privileges, so callers need no access to the models themselves |
| Catalog `prod_ai` | `MANAGE`, **or** `USE CATALOG` already granted to `account users` | So deploy can grant the app's service principal `USE CATALOG` automatically (see below) |

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
-- Model names containing hyphens must be backtick-quoted.
GRANT EXECUTE ON MODEL system.ai.`gpt-oss-20b`                   TO `<agents-deploy-application-id>`;
GRANT EXECUTE ON MODEL system.ai.`qwen3-next-80b-a3b-instruct`   TO `<agents-deploy-application-id>`;

-- One of these two, so deploy can grant the app's service principal USE CATALOG:
GRANT MANAGE ON CATALOG prod_ai TO `<agents-deploy-application-id>`;
-- or: GRANT USE CATALOG ON CATALOG prod_ai TO `account users`;
```

**Why the `prod_ai` condition.** The app's `uc_securable` resource gives the app's service
principal `EXECUTE` on the model service, and Databricks grants the parent `USE CATALOG`
and `USE SCHEMA` automatically when the resource is added. Granting `USE SCHEMA` and
`EXECUTE` is never a problem, because the deploying principal owns the schema and the
service. `USE CATALOG` on `prod_ai`, which the bundle doesn't own, is only granted if
**either** all account users already have it **or** the deploying principal has `MANAGE` on
the catalog. The Databricks docs state this condition for the Unity Catalog volume
resource; the model-service resource uses the same `uc_securable` mechanism. If you choose
neither option, grant the app's service principal `USE CATALOG` on `prod_ai` by hand after
the first deploy.

Only if you also opt into UC MCP registration (see below):

```sql
GRANT CREATE CONNECTION, CREATE SERVICE, USE SCHEMA
  ON SCHEMA prod_ai.`question-ranking-mcp` TO `<agents-deploy-application-id>`;

-- Only if you also pass a bearer token via mcp_bearer_token_secret, where the UC Secret
-- lives in <secret-catalog>.<secret-schema>:
GRANT USE CATALOG ON CATALOG <secret-catalog> TO `<agents-deploy-application-id>`;
GRANT USE SCHEMA ON SCHEMA <secret-catalog>.<secret-schema> TO `<agents-deploy-application-id>`;
GRANT READ SECRET
  ON SECRET <secret-catalog>.<secret-schema>.<secret-name> TO `<agents-deploy-application-id>`;
```

`READ SECRET` is granted on the one secret only, not on the schema, so the principal cannot
read other current or future secrets there (see the Databricks
[Unity Catalog secrets](https://docs.databricks.com/aws/en/security/secrets/unity-catalog-secrets)
docs). `READ SECRET` is the privilege that lets a principal use a secret's value; `REFERENCE SECRET` only makes it discoverable,
so it is not enough here. No `MANAGE` or ownership is needed, because the job never reads
the secret's metadata itself.

Note the backticks. Hyphens are legal in a UC schema name, and the bundle YAML takes the
literal `question-ranking-mcp`, but **SQL requires backtick-quoting**:
``prod_ai.`question-ranking-mcp` ``.

> **`run_as` does not apply here.** Bundle `run_as` only affects jobs and pipelines. The
> schema, the model service and the app are owned by whichever identity ran
> `databricks bundle deploy`. The app also gets its *own* runtime service principal,
> distinct from `agents-deploy`, which receives `EXECUTE` on the model service through the
> app's `resources` block; you do not grant it by hand.

## Deploy

```bash
# Validate first - always.
databricks bundle validate -t dev  --profile <your-profile>
databricks bundle validate -t prod --profile <prod-profile>

# Every deploy is these two steps, in this order: on the first install and after every
# change alike. Never run the first without the second.
databricks bundle deploy -t prod --profile <prod-profile>
databricks bundle run question_ranking_mcp -t prod --profile <prod-profile>

# dev: the same two steps.
databricks bundle deploy -t dev --profile <your-profile>
databricks bundle run question_ranking_mcp -t dev --profile <your-profile>

# Find the app URL.
databricks apps get mcp-question-ranking --profile <prod-profile> | jq -r .url
```

The two steps do different jobs:

1. **`bundle deploy`** creates or updates the schema, the model service, the job and the app,
   and uploads the app's source. With `lifecycle.started: true` it also starts the app's
   compute, and when the app is first created or its resource config changes, it creates a
   deployment from the uploaded source.
2. **`bundle run question_ranking_mcp`** ships the uploaded source to the app. It starts
   the app's compute if it is stopped, then always creates a new deployment from the
   bundle's synced source. `question_ranking_mcp` is the app's resource key.

`bundle deploy` alone does **not** redeploy the app when only its code changed, so a
deploy without the `run` can leave the app serving old code. The first install would
usually come up from `deploy` alone, but always running both steps is what keeps every
deploy correct. Treat `run` as part of every
deploy rather than deciding case by case. Do not use `databricks apps start` instead: it
starts compute and resumes the *last* deployment, which can bring stale code back up.

### Choosing a workspace

Both targets are **pinned** to a host in `databricks.yml`:

| Target | Host |
|---|---|
| `dev` | `https://fevm-perdomo-demos.cloud.databricks.com` |
| `prod` | `https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com` |

The CLI **rejects** a `--profile` whose host differs from the target's `workspace.host`,
so a profile can never redirect a target. In particular, `-t dev` cannot be pointed at the
production sandbox by mistake. To use a different workspace, change that target's
`workspace.host`.

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

The app's `/mcp` endpoint is protected: every request needs a Databricks **OAuth** access token in the
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
`best_question` tools instead call the model service as the app's *own* service
principal, so ranking works for any caller with CAN USE on the app, without each user
needing access to the model service or its models.

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
qualified name, `<catalog>.<schema>.<secret>`, which is the form
`connections.create(secrets=...)` documents ("UC Secret fully qualified names").

It is **not** a workspace secret scope. A `<scope>/<key>` path, with or without a
`secrets/` prefix, is a different and incompatible namespace. The job checks the format
locally before calling anything, and rejects a wrong one with an explicit message.

The job does not check that the secret exists. That check would need ownership or
`MANAGE` on the secret, more than registration itself needs. Instead,
`connections.create` validates the reference, and if it rejects it, the job stops with
the API's own error plus the grants to check: `USE CATALOG` and `USE SCHEMA` on the
secret's parents, `READ SECRET` on the secret, and `CREATE CONNECTION` on the schema.

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

At deploy time, Databricks Apps installs the app with `uv sync` from `app/uv.lock`. It
does that because `app/` contains `pyproject.toml` and `uv.lock` and **no
`requirements.txt`**. Do not add one: if `requirements.txt` exists, Apps uses pip instead
and ignores the lock. At boot, `uv run --frozen` starts the server from that environment. It never
re-resolves or rewrites the lock, and because the environment is already in sync it
normally has nothing to install.

A drift test fails if the dev environment resolves different versions of the key
packages, so the tests always run against the dependency set that actually ships. After
changing dependencies, refresh both locks:

```bash
uv lock --project app && uv lock && uv sync
```

Run the server locally:

```bash
cd app
MODEL_SERVICE=prod_ai.question-ranking-mcp.question_ranking_router uv run question-ranking-mcp
curl localhost:8000/health
```

It binds `0.0.0.0` on `DATABRICKS_APP_PORT` (**not** `PORT`), defaulting to 8000.

> `app/app.yaml` uses camelCase **`valueFrom`**, while the DABs equivalent in
> `resources/app.yml` uses snake_case **`value_from`**. Both files are real and the casing
> genuinely differs — a common source of silent misconfiguration.

## License

[Apache-2.0](LICENSE).
