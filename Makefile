PROFILE ?= accelerate-gateway
TARGET  ?= dev
FQN     ?= dev_ai.hello_mcp.hello

.PHONY: validate deploy smoke test lint destroy

validate:
	databricks bundle validate -t $(TARGET) --profile $(PROFILE)

# Deploys schema, app, connection job (runs during deploy) and the MCP service, then ships
# the app source and starts it. `bundle deploy` alone does not reliably ship app code.
deploy:
	databricks bundle deploy -t $(TARGET) --profile $(PROFILE)
	databricks bundle run hello_app -t $(TARGET) --profile $(PROFILE)

smoke:
	uv run python scripts/smoke_test.py --profile $(PROFILE) --name $(FQN)

test:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

destroy:
	databricks bundle destroy -t $(TARGET) --profile $(PROFILE)
