# Required: PROFILE=<databricks CLI profile>. Optional: CATALOG, SCHEMA, SERVICE, CONNECTION,
# APP, TARGET.
PROFILE ?=
TARGET  ?= dev
CATALOG ?= dev_ai
SCHEMA  ?= hello_mcp
SERVICE ?= hello
CONNECTION ?= hello_mcp_conn
APP     ?= mcp-hello

FQN  = $(CATALOG).$(SCHEMA).$(SERVICE)
VARS = --var catalog=$(CATALOG) --var schema=$(SCHEMA) --var mcp_service_id=$(SERVICE) \
       --var connection_name=$(CONNECTION) --var app_name=$(APP)
DBX  = databricks --profile $(PROFILE)

.PHONY: validate deploy smoke test lint destroy check-profile

check-profile:
	@test -n "$(PROFILE)" || { echo "PROFILE is not set. Usage: make $(firstword $(MAKECMDGOALS)) PROFILE=<databricks-cli-profile>" >&2; exit 1; }

validate: check-profile
	$(DBX) bundle validate -t $(TARGET) $(VARS)

# Deploys schema, app, connection job (runs during deploy) and the MCP service, then ships
# the app source and starts it. `bundle deploy` alone does not reliably ship app code.
deploy: check-profile
	$(DBX) bundle deploy -t $(TARGET) $(VARS)
	$(DBX) bundle run hello_app -t $(TARGET) $(VARS)

smoke: check-profile
	uv run python scripts/smoke_test.py --profile $(PROFILE) --name $(FQN)

test:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

# The connection is created by a job (not the bundle) and a schema that still contains it
# cannot be dropped, so: delete the MCP service, then the connection (only a confirmed
# NOT_FOUND is ignored), then let the bundle remove everything else.
destroy: check-profile
	uv run python scripts/teardown.py --profile $(PROFILE) --catalog $(CATALOG) \
		--schema $(SCHEMA) --service $(SERVICE) --connection $(CONNECTION)
	$(DBX) bundle destroy -t $(TARGET) $(VARS) --auto-approve
