# Convenience wrapper around the plain `databricks bundle ...` commands in the README.
# Required: PROFILE=<databricks CLI profile>. TARGET (dev|prod, default dev) selects the bundle
# target, whose variables are the source of truth. CATALOG, SCHEMA, SERVICE, CONNECTION and APP
# are optional explicit overrides, passed through as --var.
PROFILE ?=
TARGET  ?= dev
CATALOG ?=
SCHEMA  ?=
SERVICE ?=
CONNECTION ?=
APP     ?=

VARS = $(if $(CATALOG),--var catalog=$(CATALOG)) $(if $(SCHEMA),--var schema=$(SCHEMA)) \
       $(if $(SERVICE),--var mcp_service_id=$(SERVICE)) \
       $(if $(CONNECTION),--var connection_name=$(CONNECTION)) $(if $(APP),--var app_name=$(APP))
DBX  = databricks --profile $(PROFILE)

# Resolved variable values of the target (with overrides applied); evaluated once, on first use.
RESOLVED = $(eval RESOLVED := $(shell $(DBX) bundle validate -t $(TARGET) $(VARS) -o json 2>/dev/null | python3 -c "import sys,json;v=json.load(sys.stdin)['variables'];print(*(k+'='+v[k]['value'] for k in ('catalog','schema','mcp_service_id','connection_name')))"))$(RESOLVED)
rv = $(patsubst $(1)=%,%,$(filter $(1)=%,$(RESOLVED)))

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
	uv run python scripts/smoke_test.py --profile $(PROFILE) \
		--name $(call rv,catalog).$(call rv,schema).$(call rv,mcp_service_id)

test:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

# The connection is created by a job (not the bundle) and a schema that still contains it
# cannot be dropped, so: delete the MCP service, then the connection (only a confirmed
# NOT_FOUND is ignored), then let the bundle remove everything else.
destroy: check-profile
	uv run python scripts/teardown.py --profile $(PROFILE) --catalog $(call rv,catalog) \
		--schema $(call rv,schema) --service $(call rv,mcp_service_id) \
		--connection $(call rv,connection_name)
	$(DBX) bundle destroy -t $(TARGET) $(VARS) --auto-approve
