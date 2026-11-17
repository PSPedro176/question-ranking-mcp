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
│    + AI Gateway usage tracking & rate limit │
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
| `tests/` | 66 unit tests, no network and no Databricks auth |

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

```sql
GRANT USE CATALOG, CREATE SCHEMA ON CATALOG prod_ai TO `agents-deploy`;

GRANT USE CATALOG ON CATALOG system TO `agents-deploy`;
GRANT USE SCHEMA  ON SCHEMA  system.ai TO `agents-deploy`;
GRANT EXECUTE ON MODEL system.ai.llama_v3_2_3b_instruct  TO `agents-deploy`;
GRANT EXECUTE ON MODEL system.ai.mistral_7b_instruct_v0_2 TO `agents-deploy`;
```

Only if you also opt into UC MCP registration (see below):

```sql
GRANT CREATE CONNECTION, CREATE SERVICE, USE SCHEMA
  ON SCHEMA prod_ai.`question-ranking-mcp` TO `agents-deploy`;
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

# Deploy. `dev` uses mode: development, which prefixes resource names per user.
databricks bundle deploy -t dev  --profile <your-profile>
databricks bundle deploy -t prod --profile <prod-profile>

# Start the app (deploy creates it; it does not start it).
databricks apps start mcp-question-ranking --profile <prod-profile>

# Find the app URL.
databricks apps get mcp-question-ranking --profile <prod-profile> | jq -r .url
```

The endpoint provisions GPU capacity and can take several minutes to go green:

```bash
databricks serving-endpoints get question-ranking-router --profile <prod-profile> \
  | jq '{ready: .state.ready, config_update: .state.config_update}'
# usable once ready == READY and config_update == NOT_UPDATING
```

`dev` targets `https://fevm-perdomo-demos.cloud.databricks.com` and `prod` targets
`https://fe-sandbox-accelerate-ai-gateway.cloud.databricks.com`. Override with
`--var`-free profile selection or edit the target's `workspace.host`.

## Connect an MCP client

The server speaks **streamable HTTP** at `/mcp`:

```
https://<app-url>/mcp
```

`stateless_http=True` is set deliberately: several Databricks MCP clients (the AI
Playground and the Assistant among them) do not send an `mcp-session-id` header, and a
stateful server rejects their follow-up requests with HTTP 400.

For Claude Code, Cursor, and similar:

```json
{
  "mcpServers": {
    "question-ranking": {
      "type": "http",
      "url": "https://<app-url>/mcp"
    }
  }
}
```

In the **AI Playground**, the app appears in the MCP server picker — this is why the app
name carries the `mcp-` prefix, which the Playground uses to recognise it.

**Authentication: MCP callers must use OAuth.** Personal access tokens are not supported
for MCP requests to Databricks Apps. The app declares the `iam.current-user:read` user API
scope so Apps forwards the caller's token in `x-forwarded-access-token`, which the
`whoami` tool reads. The `rank_questions` and `best_question` tools instead call the
serving endpoint as the app's *own* service principal, so ranking works for any authorised
caller without each user needing endpoint access.

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

An Apps-hosted MCP server is exactly the source that is excluded. There is a concrete
mechanical reason: a UC HTTP connection requires a `bearer_token` option, while a
Databricks Apps MCP endpoint requires OAuth and rejects static bearer tokens.

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
  --var mcp_bearer_token_secret=<scope>/<key>

databricks bundle run register_uc_mcp -t prod --profile <prod-profile>
```

With `register_uc_mcp_service=false` (the default) the job prints the plan it *would*
execute and exits cleanly. With it set to `true` it resolves the deployed app's URL,
creates the schema-level HTTP connection, then calls `create-mcp-service`. **If Unity
Catalog rejects that call, the job fails with a message pointing back at this section.**
That is the documented limitation, not a defect in the bundle.

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

Tests cover `app/server/ranking.py` only, and that is intentional: all ranking decisions
(input validation, score coercion and clamping, JSON extraction, index-keyed matching,
fallbacks, ordering, tie-breaking, prompt construction) live there with no network,
framework, or Databricks dependency. The I/O layers (`llm.py`, `tools.py`, `app.py`) hold
no decision logic worth mocking a workspace for.

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
