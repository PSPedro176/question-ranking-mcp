from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec

import create_connection as job
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.apps import AppsAPI
from databricks.sdk.service.catalog import ConnectionsAPI
from databricks.sdk.service.oauth2 import ServicePrincipalSecretsProxyAPI

SECRET = "super-secret-value"
ARGS = [
    "--catalog", "c", "--schema", "s", "--app_name", "mcp-x", "--connection_name", "conn",
]  # fmt: skip


def make_client(*, missing=False):
    """Client whose sub-APIs are autospec'd, so signature drift in the SDK fails the tests."""
    w = MagicMock()
    w.config.host = "https://ws.example.com/"
    w.apps = create_autospec(AppsAPI, instance=True)
    w.connections = create_autospec(ConnectionsAPI, instance=True)
    w.service_principal_secrets_proxy = create_autospec(
        ServicePrincipalSecretsProxyAPI, instance=True
    )
    w.apps.get.return_value = SimpleNamespace(
        url="https://mcp-x.databricksapps.com",
        service_principal_id=42,
        service_principal_client_id="client-1",
    )
    if missing:
        w.connections.get.side_effect = NotFound("nope")
    else:
        w.connections.get.return_value = SimpleNamespace(options={})
    w.service_principal_secrets_proxy.create.return_value = SimpleNamespace(
        id="sid-1", secret=SECRET
    )
    return w


def current_options(w):
    return job.desired_options(w, w.apps.get.return_value) | {"access_token_expiration": "x"}


def no_secret_leak(capsys):
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


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
    w.connections.get.return_value = SimpleNamespace(
        options=current_options(w) | {"client_id": "old-client"}
    )
    assert job.main(ARGS, w) == 0
    w.service_principal_secrets_proxy.create.assert_called_once()
    w.connections.create.assert_not_called()
    kwargs = w.connections.update.call_args.kwargs
    assert kwargs["name"] == "c.s.conn"
    assert kwargs["options"]["client_id"] == "client-1"
    assert kwargs["options"]["client_secret"] == SECRET


def test_failed_create_revokes_secret_and_never_logs_it(capsys):
    w = make_client(missing=True)
    w.connections.create.side_effect = PermissionDenied(f"denied for {SECRET}")
    assert job.main(ARGS, w) == 1
    w.service_principal_secrets_proxy.delete.assert_called_once_with(
        service_principal_id="42", secret_id="sid-1"
    )
    no_secret_leak(capsys)


def test_failed_update_revokes_secret():
    w = make_client()
    w.connections.get.return_value = SimpleNamespace(options={"host": "https://old"})
    w.connections.update.side_effect = PermissionDenied("denied")
    assert job.main(ARGS, w) == 1
    w.service_principal_secrets_proxy.delete.assert_called_once()


def test_revocation_failure_with_non_sdk_error_is_contained_and_sanitised(capsys):
    w = make_client(missing=True)
    w.connections.create.side_effect = PermissionDenied(f"create failed with {SECRET}")
    w.service_principal_secrets_proxy.delete.side_effect = RuntimeError(f"revoke blew up {SECRET}")
    assert job.main(ARGS, w) == 1
    err = capsys.readouterr().err
    assert SECRET not in err
    assert "could not revoke secret sid-1" in err
    assert "RuntimeError" in err


def test_non_sdk_error_while_creating_still_revokes(capsys):
    w = make_client(missing=True)
    w.connections.create.side_effect = ValueError(SECRET)
    assert job.main(ARGS, w) == 1
    w.service_principal_secrets_proxy.delete.assert_called_once()
    no_secret_leak(capsys)


def test_failure_before_minting_is_a_controlled_error(capsys):
    w = make_client(missing=True)
    w.apps.get.side_effect = NotFound("no such app")
    assert job.main(ARGS, w) == 1
    w.service_principal_secrets_proxy.create.assert_not_called()
    assert "ERROR: [NotFound]" in capsys.readouterr().err
