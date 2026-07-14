"""Tests for the profile-schedule payload helpers.

These lock in the fix for the schedule endpoint mismatch: the maintained
eero-api PUTs LOWERCASE day names and a `type: bedtime` block onto the
profile's `schedule` array. The previous code sent capitalized day names with
`name`/`enabled` fields to a `/schedules` subcollection, matching neither the
endpoint nor the block shape of the reference client.
"""
from __future__ import annotations

from eero_cli.cli import _bedtime_block, _count_schedules


class TestBedtimeBlock:
    def test_lowercases_days(self):
        block = _bedtime_block(["Monday", "Tuesday", "Sunday"], "21:00", "07:00")
        assert block["days"] == ["monday", "tuesday", "sunday"]

    def test_type_is_bedtime(self):
        assert _bedtime_block(["Monday"], "21:00", "07:00")["type"] == "bedtime"

    def test_preserves_times(self):
        block = _bedtime_block(["Friday"], "22:30", "06:15")
        assert block["start"] == "22:30"
        assert block["end"] == "06:15"

    def test_drops_legacy_fields(self):
        # The old (broken) payload carried name/enabled and hit the wrong endpoint.
        block = _bedtime_block(["Monday"], "21:00", "07:00")
        assert "name" not in block
        assert "enabled" not in block

    def test_exact_shape(self):
        assert _bedtime_block(["Saturday"], "20:00", "08:00") == {
            "days": ["saturday"],
            "start": "20:00",
            "end": "08:00",
            "type": "bedtime",
        }


class TestCountSchedules:
    def test_counts_list(self):
        assert _count_schedules({"schedule": [{"a": 1}, {"b": 2}]}) == 2

    def test_missing_key(self):
        assert _count_schedules({}) == 0

    def test_non_list_is_zero(self):
        assert _count_schedules({"schedule": "nope"}) == 0

    def test_empty_list(self):
        assert _count_schedules({"schedule": []}) == 0
