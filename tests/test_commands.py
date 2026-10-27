"""Command-boundary regressions using synthetic clients, without credentials or IO."""

import json
from unittest.mock import AsyncMock

import pytest

from eero_cli import cli
from eero_cli.cli import EeroAPIException, EeroAuthenticationException, EeroException


class FakeClient:
    is_authenticated = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def client(monkeypatch):
    client = FakeClient()
    client.get_account = AsyncMock(return_value={"data": {"name": "Test User"}})
    client.get_networks = AsyncMock(return_value={"data": {"networks": []}})
    monkeypatch.setattr(cli, "_client", lambda args: client)
    return client


@pytest.mark.parametrize("json_mode", [False, True])
def test_status_local_logout_makes_no_requests(client, capsys, json_mode):
    client.is_authenticated = False
    assert cli.main(["status", *(["--json"] if json_mode else [])]) == 1
    output = capsys.readouterr()
    if json_mode:
        assert json.loads(output.out)["authenticated"] is False
    else:
        assert "False" in output.out
        assert "eero auth" in output.out
    client.get_account.assert_not_awaited()
    client.get_networks.assert_not_awaited()


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("networks", [[], [{"id": "net-1", "name": "Test Network"}]])
def test_status_valid_session_including_zero_networks(client, capsys, json_mode, networks):
    client.get_networks.return_value = {"data": {"networks": networks}}
    assert cli.main(["status", *(["--json"] if json_mode else [])]) == 0
    output = capsys.readouterr()
    if json_mode:
        result = json.loads(output.out)
        assert result == {
            "authenticated": True,
            "session_path": str(cli.DEFAULT_SESSION_PATH),
            "account": {"name": "Test User"},
            "networks": networks,
        }
    else:
        assert "True" in output.out
        assert "Test User" in output.out
        assert "networks:" in output.out
        if networks:
            assert "Test Network (net-1)" in output.out
    assert output.err == ""


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("failed_read", ["get_account", "get_networks"])
@pytest.mark.parametrize(
    ("error", "exit_code", "error_type"),
    [
        (EeroAuthenticationException("session expired"), 1, "authentication_failed"),
        (EeroAPIException(503, "service unavailable"), 3, "remote_validation_failed"),
        (EeroException("connection lost"), 3, "remote_validation_failed"),
    ],
)
def test_status_reports_remote_failures(client, capsys, json_mode, failed_read, error, exit_code, error_type):
    getattr(client, failed_read).side_effect = error
    assert cli.main(["status", *(["--json"] if json_mode else [])]) == exit_code
    output = capsys.readouterr()
    if json_mode:
        result = json.loads(output.out)
        assert result["authenticated"] is (False if exit_code == 1 else None)
        assert result["error"]["type"] == error_type
        assert str(error) in result["error"]["message"]
        if failed_read == "get_networks":
            assert result["account"] == {"name": "Test User"}
    else:
        assert str(error) in output.err
        assert "auth" in output.err if exit_code == 1 else "validation" in output.err
        assert "True" not in output.out
        if failed_read == "get_networks":
            assert "Test User" in output.out

