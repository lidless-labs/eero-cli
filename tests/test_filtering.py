"""Tests for device/profile filtering and single-match resolution.

This is the safety-critical logic: `_find_one_device` decides which device a
destructive command (block, forget) acts on, and MUST refuse to return a device
when a query is ambiguous. `_filter_devices` decides the candidate set for bulk
operations. A false match here means booting the wrong device off wifi.
"""
from __future__ import annotations

from eero_cli.cli import (
    _compact_mac,
    _filter_devices,
    _find_one_device,
    _find_one_profile,
    _matches_terms,
    _norm,
)


def dev(**kw):
    """Build a device dict with sane defaults."""
    base = {
        "url": "https://api/networks/1/devices/1",
        "nickname": "",
        "hostname": "",
        "manufacturer": "",
        "mac": "",
        "ip": "",
        "connected": False,
    }
    base.update(kw)
    return base


class TestNorm:
    def test_lowercases_and_strips(self):
        assert _norm("  HeLLo ") == "hello"

    def test_none_is_empty(self):
        assert _norm(None) == ""

    def test_number(self):
        assert _norm(42) == "42"


class TestCompactMac:
    def test_strips_colons(self):
        assert _compact_mac("BC:24:11:22:33:44") == "bc2411223344"

    def test_strips_dashes(self):
        assert _compact_mac("bc-24-11") == "bc2411"

    def test_none(self):
        assert _compact_mac(None) == ""


class TestMatchesTerms:
    def test_all_terms_present(self):
        assert _matches_terms("samsung galaxy tv", "galaxy tv") is True

    def test_missing_term(self):
        assert _matches_terms("samsung galaxy", "galaxy tv") is False

    def test_case_insensitive(self):
        assert _matches_terms("samsung galaxy", "GALAXY") is True

    def test_empty_query_matches(self):
        # Documents current behaviour: an empty query matches everything.
        assert _matches_terms("anything", "") is True


class TestFilterDevices:
    def setup_method(self):
        self.devices = [
            dev(url="https://api/d/1", nickname="Living Room TV",
                manufacturer="Samsung", mac="BC:24:11:00:00:01",
                ip="192.0.2.20", connected=True),
            dev(url="https://api/d/2", nickname="Kids iPad",
                manufacturer="Apple", mac="AA:BB:CC:00:00:02",
                ip="192.0.2.21", connected=False),
            dev(url="https://api/d/3", hostname="printer-hp",
                manufacturer="HP", mac="BC:24:11:00:00:03",
                ip="192.0.2.22", connected=False),
        ]

    def test_no_filters_returns_all(self):
        assert len(_filter_devices(self.devices)) == 3

    def test_name_regex_matches_manufacturer(self):
        out = _filter_devices(self.devices, name_regex="samsung")
        assert [d["mac"] for d in out] == ["BC:24:11:00:00:01"]

    def test_name_regex_matches_hostname(self):
        out = _filter_devices(self.devices, name_regex="printer")
        assert [d["url"] for d in out] == ["https://api/d/3"]

    def test_search_multi_term(self):
        out = _filter_devices(self.devices, search="samsung tv")
        assert len(out) == 1

    def test_mac_prefix_normalized(self):
        out = _filter_devices(self.devices, mac_prefix="bc:24:11")
        assert {d["url"] for d in out} == {"https://api/d/1", "https://api/d/3"}

    def test_mac_prefix_compact_form(self):
        out = _filter_devices(self.devices, mac_prefix="bc2411")
        assert len(out) == 2

    def test_only_offline(self):
        out = _filter_devices(self.devices, only_offline=True)
        assert all(not d["connected"] for d in out)
        assert len(out) == 2

    def test_only_online(self):
        out = _filter_devices(self.devices, only_online=True)
        assert [d["url"] for d in out] == ["https://api/d/1"]

    def test_combined_filters(self):
        out = _filter_devices(self.devices, mac_prefix="bc2411", only_offline=True)
        assert [d["url"] for d in out] == ["https://api/d/3"]


class TestFindOneDevice:
    def setup_method(self):
        self.devices = [
            dev(url="https://api/d/42", nickname="Johnny PC", mac="BC:24:11:00:00:42"),
            dev(url="https://api/d/43", nickname="Johnny Phone", mac="AA:BB:CC:00:00:43"),
        ]

    def test_exact_by_device_id(self):
        m = _find_one_device(self.devices, "42")
        assert m is not None and m["url"].endswith("/42")

    def test_exact_by_mac(self):
        m = _find_one_device(self.devices, "AA:BB:CC:00:00:43")
        assert m is not None and m["url"].endswith("/43")

    def test_exact_by_compact_mac(self):
        m = _find_one_device(self.devices, "aabbcc000043")
        assert m is not None and m["url"].endswith("/43")

    def test_exact_by_label(self):
        m = _find_one_device(self.devices, "Johnny Phone")
        assert m is not None and m["url"].endswith("/43")

    def test_no_match_returns_none(self, capsys):
        assert _find_one_device(self.devices, "nonexistent") is None
        assert "No device matches" in capsys.readouterr().err

    def test_ambiguous_returns_none(self, capsys):
        # "Johnny" matches both by fuzzy terms -> must refuse, not guess.
        assert _find_one_device(self.devices, "Johnny") is None
        assert "matched 2 devices" in capsys.readouterr().err

    def test_fuzzy_single_match(self):
        m = _find_one_device(self.devices, "PC")
        assert m is not None and m["url"].endswith("/42")


class TestFindOneProfile:
    def setup_method(self):
        self.profiles = [
            {"id": "10", "name": "Johnny", "url": "https://api/p/10"},
            {"id": "11", "name": "Guest", "url": "https://api/p/11"},
        ]

    def test_exact_by_id(self):
        m = _find_one_profile(self.profiles, "10")
        assert m is not None and m["name"] == "Johnny"

    def test_exact_by_name(self):
        m = _find_one_profile(self.profiles, "Guest")
        assert m is not None and m["id"] == "11"

    def test_no_match(self, capsys):
        assert _find_one_profile(self.profiles, "zzz") is None
        assert "No profile matches" in capsys.readouterr().err
