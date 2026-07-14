"""Tests for label/time formatting and input validation helpers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eero_cli.cli import (
    _compile_pattern,
    _device_label,
    _fmt_last_active,
    _parse_days,
    _profile_label,
    _validate_time,
)


class TestFmtLastActive:
    def test_falsy_is_never(self):
        assert _fmt_last_active(None) == "never"
        assert _fmt_last_active("") == "never"

    def test_seconds_ago(self):
        dt = datetime.now(UTC) - timedelta(seconds=5)
        assert _fmt_last_active(dt).endswith("s ago")

    def test_minutes_ago(self):
        dt = datetime.now(UTC) - timedelta(minutes=3, seconds=1)
        assert _fmt_last_active(dt) == "3m ago"

    def test_hours_ago(self):
        dt = datetime.now(UTC) - timedelta(hours=2, seconds=1)
        assert _fmt_last_active(dt) == "2h ago"

    def test_days_ago(self):
        dt = datetime.now(UTC) - timedelta(days=3, seconds=1)
        assert _fmt_last_active(dt) == "3d ago"

    def test_iso_string_with_z(self):
        assert _fmt_last_active("2020-01-01T00:00:00Z").endswith("d ago")

    def test_naive_iso_assumed_utc(self):
        # No tzinfo -> treated as UTC, must not raise.
        assert _fmt_last_active("2020-01-01T00:00:00").endswith("d ago")

    def test_unparseable_string_returned_asis(self):
        assert _fmt_last_active("not-a-date") == "not-a-date"


class TestLabels:
    def test_device_nickname_wins(self):
        assert _device_label({"nickname": "A", "hostname": "B"}) == "A"

    def test_device_falls_back_to_hostname(self):
        assert _device_label({"hostname": "B", "manufacturer": "C"}) == "B"

    def test_device_falls_back_to_manufacturer(self):
        assert _device_label({"manufacturer": "C", "mac": "m"}) == "C"

    def test_device_unknown(self):
        assert _device_label({}) == "(unknown)"

    def test_profile_name_wins(self):
        assert _profile_label({"name": "X", "nickname": "Y"}) == "X"

    def test_profile_unnamed(self):
        assert _profile_label({}) == "(unnamed)"


class TestParseDays:
    def test_all(self):
        assert _parse_days("all") == [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
        ]

    def test_weekdays(self):
        assert _parse_days("weekdays") == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    def test_weekends_alias(self):
        assert _parse_days("weekend") == ["Saturday", "Sunday"]
        assert _parse_days("weekends") == ["Saturday", "Sunday"]

    def test_comma_list_normalized(self):
        assert _parse_days("monday,TUESDAY") == ["Monday", "Tuesday"]

    def test_invalid_day_exits(self):
        with pytest.raises(SystemExit) as exc:
            _parse_days("funday")
        assert "invalid day" in str(exc.value)


class TestValidateTime:
    def test_valid(self):
        assert _validate_time("21:00") == "21:00"

    def test_invalid_hour_exits(self):
        with pytest.raises(SystemExit):
            _validate_time("25:00")

    def test_garbage_exits(self):
        with pytest.raises(SystemExit):
            _validate_time("nope")


class TestCompilePattern:
    def test_valid_regex(self):
        pat = _compile_pattern("^bc24")
        assert pat.search("bc2411") is not None

    def test_case_insensitive(self):
        pat = _compile_pattern("SAMSUNG")
        assert pat.search("samsung tv") is not None

    def test_invalid_regex_exits(self):
        with pytest.raises(SystemExit) as exc:
            _compile_pattern("[unterminated")
        assert "invalid regex" in str(exc.value)
