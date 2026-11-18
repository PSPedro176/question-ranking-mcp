import shutil
import subprocess
from unittest.mock import MagicMock, create_autospec

import pytest
import teardown
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.catalog import ConnectionsAPI

ARGS = ["--catalog", "c", "--schema", "s", "--service", "svc", "--connection", "conn"]


def make_client():
    w = MagicMock()
    w.connections = create_autospec(ConnectionsAPI, instance=True)
    return w


def test_deletes_service_before_connection():
    w = make_client()
    order = MagicMock()
    order.attach_mock(w.ai_gateway.delete_mcp_service, "service")
    order.attach_mock(w.connections.delete, "connection")
    assert teardown.main(ARGS, w) == 0
    assert [c[0] for c in order.mock_calls] == ["service", "connection"]
    w.ai_gateway.delete_mcp_service.assert_called_once_with(name="mcp-services/c.s.svc")
    w.connections.delete.assert_called_once_with(name="c.s.conn")


def test_not_found_is_ignored():
    w = make_client()
    w.ai_gateway.delete_mcp_service.side_effect = NotFound("gone")
    w.connections.delete.side_effect = NotFound("gone")
    assert teardown.main(ARGS, w) == 0


def test_other_errors_propagate():
    w = make_client()
    w.ai_gateway.delete_mcp_service.side_effect = PermissionDenied("no")
    with pytest.raises(PermissionDenied):
        teardown.main(ARGS, w)
    w.connections.delete.assert_not_called()


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_makefile_requires_profile_and_orders_destroy():
    bad = subprocess.run(["make", "check-profile", "PROFILE="], capture_output=True, text=True)
    assert bad.returncode != 0 and "PROFILE is not set" in bad.stderr
    ok = subprocess.run(["make", "-n", "destroy", "PROFILE=p"], capture_output=True, text=True)
    assert ok.returncode == 0
    teardown_at = ok.stdout.index("scripts/teardown.py")
    assert teardown_at < ok.stdout.index("bundle destroy")
