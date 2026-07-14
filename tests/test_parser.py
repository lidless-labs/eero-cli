"""Smoke tests for the argparse wiring.

These lock in the command surface: every subcommand resolves to its handler,
required args are enforced, and the global options parse. They would catch an
accidental rename or a dropped `set_defaults(func=...)`.
"""
from __future__ import annotations

import pytest

from eero_cli import cli
from eero_cli.cli import _build_parser


def parse(argv):
    return _build_parser().parse_args(argv)


class TestSubcommandWiring:
    def test_devices_maps_to_handler(self):
        args = parse(["devices"])
        assert args.func is cli._list_devices

    def test_auth_identifier(self):
        args = parse(["auth", "+15551234567"])
        assert args.func is cli._auth
        assert args.identifier == "+15551234567"

    def test_auth_code_only(self):
        args = parse(["auth", "--code", "123456"])
        assert args.identifier is None
        assert args.code == "123456"

    def test_block_with_unblock(self):
        args = parse(["block", "aa:bb:cc", "--unblock"])
        assert args.func is cli._block
        assert args.device == "aa:bb:cc"
        assert args.unblock is True

    def test_block_cleanup_flags(self):
        args = parse(["block-cleanup", "^bc24", "-y", "--include-online", "--unblock"])
        assert args.func is cli._block_cleanup
        assert args.pattern == "^bc24"
        assert args.yes is True
        assert args.include_online is True
        assert args.unblock is True

    def test_profile_assign_multiple_devices(self):
        args = parse(["profile-assign", "Alistair", "Samsung", "iPad"])
        assert args.func is cli._assign_profile_device
        assert args.profile == "Alistair"
        assert args.devices == ["Samsung", "iPad"]

    def test_profile_schedule_requires_start_end(self):
        args = parse(["profile-schedule", "Kids", "--start", "21:00", "--end", "08:00"])
        assert args.start == "21:00"
        assert args.end == "08:00"
        assert args.days == "all"  # default

    def test_profile_block_apps_variadic(self):
        args = parse(["profile-block-apps", "Kids", "youtube", "tiktok", "--append"])
        assert args.applications == ["youtube", "tiktok"]
        assert args.append is True

    def test_status_maps_to_handler(self):
        args = parse(["status"])
        assert args.func is cli._status
        assert args.json is False

    def test_status_json(self):
        assert parse(["status", "--json"]).json is True

    def test_rename_positional_args(self):
        args = parse(["rename", "aa:bb:cc", "Living Room TV"])
        assert args.func is cli._rename_device
        assert args.device == "aa:bb:cc"
        assert args.nickname == "Living Room TV"

    def test_devices_json_flag(self):
        assert parse(["devices", "--json"]).json is True
        assert parse(["devices"]).json is False

    def test_profiles_json_flag(self):
        assert parse(["profiles", "--json", "--devices"]).json is True


class TestGlobalOptions:
    def test_network_id_global(self):
        args = parse(["--network-id", "12345", "devices"])
        assert args.network_id == "12345"

    def test_session_path_global(self):
        args = parse(["--session-path", "/tmp/s.json", "devices"])
        assert str(args.session_path) == "/tmp/s.json"


class TestParserErrors:
    def test_missing_subcommand_exits(self):
        with pytest.raises(SystemExit):
            parse([])

    def test_unknown_subcommand_exits(self):
        with pytest.raises(SystemExit):
            parse(["frobnicate"])

    def test_profile_schedule_missing_required_exits(self):
        with pytest.raises(SystemExit):
            parse(["profile-schedule", "Kids"])  # no --start/--end
