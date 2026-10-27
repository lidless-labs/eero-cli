"""Command-boundary regressions using synthetic clients, without credentials or IO."""

import json
from unittest.mock import AsyncMock, Mock

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


@pytest.fixture
def schedule_client(client):
    client.get_networks.return_value = {"data": {"networks": [{"id": "net-1"}]}}
    client.get_profiles = AsyncMock(return_value={"data": [{"id": "profile-1", "name": "Test Profile"}]})
    client.get_profile = AsyncMock(return_value={"data": {"schedule": [
        {"days": ["monday", "tuesday"], "start": "20:15", "end": "06:30", "type": "bedtime"},
        {"days": ["saturday"], "start": "13:00", "end": "14:00", "name": "Study", "enabled": False},
    ]}})
    client.set_profile_schedule = AsyncMock()
    return client


SCHEDULE_COMMAND = ["profile-schedule", "Test Profile", "--start", "21:00", "--end", "08:00", "--days", "Friday"]


def assert_schedule_preview(output):
    assert "2 existing schedule block(s)" in output
    for detail in ("monday", "tuesday", "20:15", "06:30", "bedtime", "saturday", "13:00", "14:00", "Study"):
        assert detail in output
    assert "enabled" in output and "false" in output.lower()
    assert "replaces the entire schedule" in output
    assert "21:00-08:00 on Friday" in output


@pytest.mark.parametrize("reply", ["y", "n", None], ids=["confirm", "cancel", "yes-flag"])
def test_schedule_previews_replacement_before_prompt_or_write(schedule_client, monkeypatch, capsys, reply):
    def confirm(prompt):
        schedule_client.get_profile.assert_awaited_once()
        assert_schedule_preview(capsys.readouterr().out)
        schedule_client.set_profile_schedule.assert_not_awaited()
        return reply

    prompt = Mock(side_effect=confirm)
    monkeypatch.setattr("builtins.input", prompt)

    async def write(**kwargs):
        schedule_client.get_profile.assert_awaited_once()
        if reply is None:
            assert_schedule_preview(capsys.readouterr().out)
        else:
            prompt.assert_called_once()

    schedule_client.set_profile_schedule.side_effect = write
    assert cli.main([*SCHEDULE_COMMAND, *(["--yes"] if reply is None else [])]) == 0
    output = capsys.readouterr().out
    if reply == "n":
        assert "aborted" in output
        schedule_client.set_profile_schedule.assert_not_awaited()
    else:
        schedule_client.set_profile_schedule.assert_awaited_once_with(
            profile_id="profile-1", network_id="net-1",
            time_blocks=[{"days": ["friday"], "start": "21:00", "end": "08:00", "type": "bedtime"}],
        )
    if reply is None:
        prompt.assert_not_called()


@pytest.mark.parametrize("yes", [False, True])
def test_schedule_read_failure_prevents_prompt_and_write(schedule_client, monkeypatch, capsys, yes):
    schedule_client.get_profile.side_effect = EeroAPIException(503, "schedule unavailable")
    prompt = Mock(return_value="y")
    monkeypatch.setattr("builtins.input", prompt)
    assert cli.main([*SCHEDULE_COMMAND, *(["--yes"] if yes else [])]) == 3
    assert "schedule unavailable" in capsys.readouterr().err
    prompt.assert_not_called()
    schedule_client.set_profile_schedule.assert_not_awaited()


def test_schedule_with_no_existing_blocks_still_previews(schedule_client, monkeypatch, capsys):
    schedule_client.get_profile.return_value = {"data": {}}
    prompt = Mock()
    monkeypatch.setattr("builtins.input", prompt)

    async def write(**kwargs):
        output = capsys.readouterr().out
        assert "0 existing schedule block(s)" in output
        assert "replaces the entire schedule" in output
        assert "21:00-08:00 on Friday" in output

    schedule_client.set_profile_schedule.side_effect = write
    assert cli.main([*SCHEDULE_COMMAND, "--yes"]) == 0
    prompt.assert_not_called()
    schedule_client.set_profile_schedule.assert_awaited_once()
