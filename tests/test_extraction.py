"""Tests for the response-shape extraction helpers.

The eero cloud API wraps payloads inconsistently ({"data": {...}} vs
{"data": [...]} vs nested {"data": {"key": {"data": [...]}}}). These helpers
flatten all of that, so they are the load-bearing glue between the raw API and
every command. A regression here silently drops devices/networks from results.
"""
from __future__ import annotations

from eero_cli.cli import (
    _device_id_from_url,
    _device_urls,
    _extract_data_list,
    _extract_networks,
    _network_id_of,
    _profile_id_from_url,
)


class TestExtractDataList:
    def test_data_is_list_keeps_only_dicts(self):
        resp = {"data": [{"a": 1}, "junk", {"b": 2}, 3]}
        assert _extract_data_list(resp) == [{"a": 1}, {"b": 2}]

    def test_data_is_dict_with_named_key(self):
        resp = {"data": {"devices": [{"id": 1}]}}
        assert _extract_data_list(resp, "devices") == [{"id": 1}]

    def test_data_is_dict_with_nested_data_under_key(self):
        resp = {"data": {"devices": {"data": [{"id": 1}]}}}
        assert _extract_data_list(resp, "devices") == [{"id": 1}]

    def test_bare_list_input(self):
        assert _extract_data_list([{"a": 1}]) == [{"a": 1}]

    def test_unknown_key_returns_empty(self):
        assert _extract_data_list({"data": {"nope": 1}}, "devices") == []

    def test_non_dict_non_list_returns_empty(self):
        assert _extract_data_list("garbage") == []
        assert _extract_data_list(None) == []

    def test_first_matching_key_wins(self):
        resp = {"data": {"a": [{"x": 1}], "b": [{"y": 2}]}}
        assert _extract_data_list(resp, "a", "b") == [{"x": 1}]


class TestExtractNetworks:
    def test_networks_under_data(self):
        assert _extract_networks({"data": {"networks": [{"id": 1}]}}) == [{"id": 1}]

    def test_data_is_list(self):
        assert _extract_networks({"data": [{"id": 1}]}) == [{"id": 1}]

    def test_networks_nested_data(self):
        resp = {"data": {"networks": {"data": [{"id": 2}]}}}
        assert _extract_networks(resp) == [{"id": 2}]

    def test_empty(self):
        assert _extract_networks({}) == []
        assert _extract_networks({"data": {"networks": {}}}) == []


class TestNetworkIdOf:
    def test_explicit_id(self):
        assert _network_id_of({"id": 123}) == "123"

    def test_id_from_url_when_missing(self):
        assert _network_id_of({"url": "/2.2/networks/456/"}) == "456"

    def test_none_when_absent(self):
        assert _network_id_of({}) is None

    def test_id_takes_precedence_over_url(self):
        assert _network_id_of({"id": 1, "url": "/networks/999"}) == "1"


class TestIdFromUrl:
    def test_device_id_trailing_slash(self):
        assert _device_id_from_url("https://api/networks/1/devices/42/") == "42"

    def test_device_id_no_trailing_slash(self):
        assert _device_id_from_url("https://api/networks/1/devices/42") == "42"

    def test_profile_id(self):
        assert _profile_id_from_url("/2.2/networks/1/profiles/7") == "7"

    def test_empty_string(self):
        assert _device_id_from_url("") == ""


class TestDeviceUrls:
    def test_dedupes_preserving_order(self):
        resp = {"data": {"devices": [{"url": "a"}, {"url": "b"}, {"url": "a"}]}}
        assert _device_urls(resp) == ["a", "b"]

    def test_no_data_wrapper(self):
        assert _device_urls({"devices": [{"url": "a"}]}) == ["a"]

    def test_skips_non_dicts_and_missing_urls(self):
        resp = {"data": {"devices": [{"url": "a"}, "junk", {"no_url": 1}]}}
        assert _device_urls(resp) == ["a"]

    def test_empty(self):
        assert _device_urls({}) == []
