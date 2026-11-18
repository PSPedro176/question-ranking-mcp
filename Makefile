# Required: PROFILE=<databricks CLI profile>. Optional: CATALOG, SCHEMA, SERVICE, TARGET.
PROFILE ?=
TARGET  ?= dev
CATALOG ?= dev_ai
SCHEMA  ?= hello_mcp
SERVICE ?= hello
CONNECTION ?= hello_mcp_conn

FQN  = $(CATALOG).$(SCHEMA).$(SERVICE)
VARS = --var catalog=$(CATALOG) --var schema=$(SCHEMA) --var mcp_service_id=$(SERVICE) \
       --var connection_name=$(CONNECTION)
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

# The connection is created by a job, not by the bundle, so the bundle cannot remove it.
# Order: bundle destroy first (drops the MCP service, then the rest), then the connection.
destroy: check-profile
	$(DBX) bundle destroy -t $(TARGET) $(VARS) --auto-approve
	$(DBX) connections delete $(CATALOG).$(SCHEMA).$(CONNECTION) 2>&1 | grep -v "does not exist" || true
