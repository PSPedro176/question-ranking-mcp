from types import SimpleNamespace
from unittest.mock import MagicMock

import create_connection as job
from databricks.sdk.errors import NotFound, PermissionDenied

SECRET = "super-secret-value"
ARGS = [
    "--catalog", "c", "--schema", "s", "--app_name", "mcp-x", "--connection_name", "conn",
]  # fmt: skip


def make_client(existing_options=None, *, missing=False):
    w = MagicMock()
    w.config.host = "https://ws.example.com/"
    w.apps.get.return_value = SimpleNamespace(
        url="https://mcp-x.databricksapps.com",
        service_principal_id=42,
        service_principal_client_id="client-1",
    )
    if missing:
        w.connections.get.side_effect = NotFound("nope")
    else:
        w.connections.get.return_value = SimpleNamespace(options=existing_options)
    w.service_principal_secrets_proxy.create.return_value = SimpleNamespace(
        id="sid-1", secret=SECRET
    )
    return w


def current_options(w):
    return job.desired_options(w, w.apps.get.return_value) | {"access_token_expiration": "x"}


def test_missing_connection_is_created_with_new_secret():
    w = make_client(missing=True)
    assert job.main(ARGS, w) == 0
    w.service_principal_secrets_proxy.create.assert_called_once()
    kwargs = w.connections.create.call_args.kwargs
    assert kwargs["parent"] == "schemas/c.s"
    assert kwargs["options"]["client_secret"] == SECRET
    assert kwargs["options"]["host"] == "https://mcp-x.databricksapps.com"
    w.service_principal_secrets_proxy.delete.assert_not_called()


def test_current_connection_mints_nothing():
    w = make_client()
    w.connections.get.return_value = SimpleNamespace(options=current_options(w))
    assert job.main(ARGS, w) == 0
    w.service_principal_secrets_proxy.create.assert_not_called()
    w.connections.create.assert_not_called()
    w.connections.update.assert_not_called()


def test_stale_connection_is_updated_with_new_secret():
    w = make_client()
    stale = current_options(w) | {"client_id": "old-client"}
    w.connections.get.return_value = SimpleNamespace(options=stale)
    assert job.main(ARGS, w) == 0
    w.service_principal_secrets_proxy.create.assert_called_once()
    w.connections.create.assert_not_called()
    name = w.connections.update.call_args.kwargs["name"]
    options = w.connections.update.call_args.kwargs["options"]
    assert name == "c.s.conn"
    assert options["client_id"] == "client-1"
    assert options["client_secret"] == SECRET


def test_failed_create_revokes_secret_and_never_logs_it(capsys):
    w = make_client(missing=True)
    w.connections.create.side_effect = PermissionDenied(f"denied for {SECRET}")
    assert job.main(ARGS, w) == 1
    w.service_principal_secrets_proxy.delete.assert_called_once_with(
        service_principal_id="42", secret_id="sid-1"
    )
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_failed_update_revokes_secret():
    w = make_client()
    w.connections.get.return_value = SimpleNamespace(options={"host": "https://old"})
    w.connections.update.side_effect = PermissionDenied("denied")
    assert job.main(ARGS, w) == 1
    w.service_principal_secrets_proxy.delete.assert_called_once()
