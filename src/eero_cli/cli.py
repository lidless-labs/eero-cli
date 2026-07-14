"""eero-cli command-line interface.

Wraps fulviofreitas/eero-api with two things it doesn't ship:
  1. A device-forget call (DELETE /networks/<nid>/devices/<did>) — eero's app
     uses this for the "Forget Device" button. Only works on offline devices.
  2. A bulk cleanup command for clearing stale entries by hostname/MAC pattern.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eero.client import EeroClient
from eero.exceptions import (
    EeroAPIException,
    EeroAuthenticationException,
    EeroException,
)

DEFAULT_SESSION_PATH = Path.home() / ".config" / "eero" / "session.json"


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise SystemExit(f"invalid regex {pattern!r}: {e}") from e


def _ensure_session_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


async def _forget_device(client: EeroClient, network_id: str, device_id: str) -> dict[str, Any]:
    """DELETE /networks/<nid>/devices/<did> via the underlying DevicesAPI."""
    devices_api = client._api.devices
    auth_token = await devices_api._auth_api.get_auth_token()
    if not auth_token:
        raise EeroAuthenticationException("Not authenticated")
    response = await devices_api.delete(
        f"networks/{network_id}/devices/{device_id}",
        auth_token=auth_token,
    )
    client._invalidate_device_cache(network_id, device_id)
    return response


def _device_id_from_url(device_url: str) -> str:
    return device_url.rstrip("/").rsplit("/", 1)[-1]


def _profile_id_from_url(profile_url: str) -> str:
    return profile_url.rstrip("/").rsplit("/", 1)[-1]


def _extract_data_list(resp: Any, *keys: str) -> list[dict[str, Any]]:
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict) and isinstance(value.get("data"), list):
                return [x for x in value["data"] if isinstance(x, dict)]
    return []


def _extract_networks(networks_resp: dict) -> list[dict]:
    """get_networks() returns {data: {networks: [...]}}; flatten that here."""
    data = networks_resp.get("data", {})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        nets = data.get("networks", [])
        if isinstance(nets, list):
            return nets
        if isinstance(nets, dict):
            inner = nets.get("data", [])
            return inner if isinstance(inner, list) else []
    return []


def _network_id_of(n: dict) -> str | None:
    nid = n.get("id")
    if nid is None and n.get("url"):
        nid = n["url"].rstrip("/").rsplit("/", 1)[-1]
    return str(nid) if nid is not None else None


async def _resolve_network_id(client: EeroClient, requested: str | None = None, *, destructive: bool = False) -> str:
    networks_resp = await client.get_networks()
    networks = _extract_networks(networks_resp)
    if not networks:
        raise SystemExit("No networks found on this eero account.")
    if requested:
        for n in networks:
            if _network_id_of(n) == str(requested):
                return str(requested)
        names = ", ".join(f"{n.get('name')}={_network_id_of(n)}" for n in networks)
        raise SystemExit(f"--network-id {requested} not in account. Visible: {names}")
    if len(networks) > 1:
        names = ", ".join(f"{n.get('name')}={_network_id_of(n)}" for n in networks)
        if destructive:
            raise SystemExit(
                f"account has {len(networks)} networks ({names}); "
                "pass --network-id <id> to choose explicitly for destructive commands"
            )
        print(f"warning: account has {len(networks)} networks ({names}); using first", file=sys.stderr)
    nid = _network_id_of(networks[0])
    if nid is None:
        raise SystemExit(f"Could not extract network id from: {networks[0]}")
    return nid


def _fmt_last_active(value: Any) -> str:
    if not value:
        return "never"
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    elif isinstance(value, datetime):
        dt = value
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _device_label(device: dict[str, Any]) -> str:
    return (
        device.get("nickname")
        or device.get("hostname")
        or device.get("manufacturer")
        or device.get("mac")
        or "(unknown)"
    )


def _profile_label(profile: dict[str, Any]) -> str:
    return (
        profile.get("name")
        or profile.get("nickname")
        or profile.get("profile_name")
        or profile.get("url")
        or "(unnamed)"
    )


def _norm(value: Any) -> str:
    return str(value or "").lower().strip()


def _compact_mac(value: Any) -> str:
    return _norm(value).replace(":", "").replace("-", "")


def _device_search_text(device: dict[str, Any]) -> str:
    did = _device_id_from_url(device.get("url", ""))
    fields = [
        _device_label(device),
        device.get("nickname"),
        device.get("hostname"),
        device.get("manufacturer"),
        device.get("mac"),
        _compact_mac(device.get("mac")),
        device.get("ip"),
        device.get("url"),
        did,
    ]
    return " ".join(_norm(x) for x in fields if x)


def _profile_search_text(profile: dict[str, Any]) -> str:
    pid = _profile_id_from_url(profile.get("url", ""))
    fields = [
        _profile_label(profile),
        profile.get("name"),
        profile.get("nickname"),
        profile.get("url"),
        profile.get("id"),
        pid,
    ]
    return " ".join(_norm(x) for x in fields if x)


def _matches_terms(text: str, query: str) -> bool:
    terms = [_norm(t) for t in re.split(r"\s+", query) if t.strip()]
    return all(t in text for t in terms)


def _filter_devices(
    devices: list[dict[str, Any]],
    name_regex: str | None = None,
    search: str | None = None,
    mac_prefix: str | None = None,
    only_offline: bool = False,
    only_online: bool = False,
) -> list[dict[str, Any]]:
    result = devices
    if name_regex:
        pat = re.compile(name_regex, re.IGNORECASE)
        result = [d for d in result if pat.search(_device_search_text(d))]
    if search:
        result = [d for d in result if _matches_terms(_device_search_text(d), search)]
    if mac_prefix:
        norm = mac_prefix.lower().replace(":", "").replace("-", "")
        result = [
            d for d in result
            if (d.get("mac") or "").lower().replace(":", "").replace("-", "").startswith(norm)
        ]
    if only_offline:
        result = [d for d in result if not d.get("connected")]
    if only_online:
        result = [d for d in result if d.get("connected")]
    return result


def _find_one_device(devices: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    target = _norm(query)
    norm_target = _compact_mac(query)
    exact = []
    for d in devices:
        did = _device_id_from_url(d.get("url", ""))
        mac = _norm(d.get("mac"))
        if target in {did.lower(), mac, _norm(_device_label(d))} or norm_target == _compact_mac(mac):
            exact.append(d)
    matches = exact or [d for d in devices if _matches_terms(_device_search_text(d), query)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"No device matches '{query}'", file=sys.stderr)
        return None
    print(f"'{query}' matched {len(matches)} devices; narrow the search:", file=sys.stderr)
    _print_device_table(matches)
    return None


def _find_one_profile(profiles: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    target = _norm(query)
    exact = []
    for p in profiles:
        pid = str(p.get("id") or _profile_id_from_url(p.get("url", ""))).lower()
        if target in {pid, _norm(_profile_label(p))}:
            exact.append(p)
    matches = exact or [p for p in profiles if _matches_terms(_profile_search_text(p), query)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"No profile matches '{query}'", file=sys.stderr)
        return None
    print(f"'{query}' matched {len(matches)} profiles; narrow the search:", file=sys.stderr)
    _print_profile_table(matches)
    return None


def _print_device_table(devices: Iterable[dict[str, Any]]) -> None:
    rows = list(devices)
    if not rows:
        print("(no devices match)")
        return
    header = f"{'STATE':<8} {'MAC':<18} {'IP':<16} {'LAST_ACTIVE':<12} {'NAME'}"
    print(header)
    print("-" * len(header))
    for d in rows:
        state = "online" if d.get("connected") else "offline"
        mac = d.get("mac") or "?"
        ip = d.get("ip") or "-"
        last = _fmt_last_active(d.get("last_active"))
        name = _device_label(d)
        print(f"{state:<8} {mac:<18} {ip:<16} {last:<12} {name}")
    print(f"\n{len(rows)} device(s)")


def _print_profile_table(profiles: Iterable[dict[str, Any]]) -> None:
    rows = list(profiles)
    if not rows:
        print("(no profiles match)")
        return
    header = f"{'ID':<10} {'DEVICES':<7} {'NAME'}"
    print(header)
    print("-" * len(header))
    for p in rows:
        pid = str(p.get("id") or _profile_id_from_url(p.get("url", "")) or "?")
        raw_devices = p.get("devices")
        devices = raw_devices if isinstance(raw_devices, list) else []
        print(f"{pid:<10} {len(devices):<7} {_profile_label(p)}")
    print(f"\n{len(rows)} profile(s)")


def _device_urls(profile_resp: Any) -> list[str]:
    data = profile_resp.get("data", profile_resp) if isinstance(profile_resp, dict) else profile_resp
    devices = data.get("devices", []) if isinstance(data, dict) else []
    urls = []
    seen = set()
    for d in devices if isinstance(devices, list) else []:
        if isinstance(d, dict) and d.get("url") and d["url"] not in seen:
            urls.append(d["url"])
            seen.add(d["url"])
    return urls


def _parse_days(days: str) -> list[str]:
    groups = {
        "all": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "weekends": ["Saturday", "Sunday"],
        "weekend": ["Saturday", "Sunday"],
    }
    key = days.lower().strip()
    if key in groups:
        return groups[key]
    by_lower = {d.lower(): d for d in groups["all"]}
    parsed = [d.strip().lower() for d in days.split(",") if d.strip()]
    bad = [d for d in parsed if d not in by_lower]
    if bad:
        raise SystemExit(f"invalid day(s): {', '.join(bad)}")
    return [by_lower[d] for d in parsed]


def _validate_time(value: str) -> str:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        raise SystemExit(f"invalid time {value!r}; use 24-hour HH:MM") from None
    return value


async def _get_profile_data(client: EeroClient, network_id: str, profile_id: str) -> dict[str, Any]:
    resp = await client.get_profile(profile_id=profile_id, network_id=network_id, refresh_cache=True)
    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    return data if isinstance(data, dict) else {}


def _count_schedules(profile_data: dict[str, Any]) -> int:
    """Number of schedule blocks on a profile (the eero `schedule` array)."""
    schedules = profile_data.get("schedule")
    return len(schedules) if isinstance(schedules, list) else 0


def _bedtime_block(days: list[str], start: str, end: str) -> dict[str, Any]:
    """Build one bedtime time-block for the eero schedule API.

    Day names are lowercased to match the format the maintained eero-api uses.
    The block replaces the whole profile schedule via `client.set_profile_schedule`
    (PUT networks/<nid>/profiles/<pid> with `{"schedule": [block]}`). The previous
    code POSTed capitalized day names with `name`/`enabled` fields to a
    `/schedules` subcollection, which matches neither the endpoint nor the block
    shape of the reference client.
    """
    return {
        "days": [d.lower() for d in days],
        "start": start,
        "end": end,
        "type": "bedtime",
    }


def _dump_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, sort_keys=True))


def _account_summary(account_data: dict[str, Any]) -> dict[str, str]:
    """Pull the human-relevant identity fields out of the /account response.

    eero wraps some fields as ``{"value": ...}``; unwrap those. Missing fields
    are omitted, so a script can treat key presence as 'known'.
    """
    def _val(v: Any) -> str | None:
        if isinstance(v, dict):
            v = v.get("value")
        return str(v) if v else None

    out: dict[str, str] = {}
    for key in ("name", "email", "phone"):
        val = _val(account_data.get(key))
        if val:
            out[key] = val
    return out


async def _list_devices(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id)
        resp = await client.get_devices(network_id=nid)
        devices = _extract_data_list(resp, "devices")
        if args.filter:
            _compile_pattern(args.filter)  # surface regex errors early
        filtered = _filter_devices(
            devices,
            name_regex=args.filter,
            search=args.search,
            mac_prefix=args.mac,
            only_offline=args.offline,
            only_online=args.online,
        )
        if args.json:
            _dump_json(filtered)
        else:
            _print_device_table(filtered)
    return 0


async def _block(args: argparse.Namespace) -> int:
    """Block a single device by ID or MAC. Survives reboots; the eero will
    refuse to give the MAC an IP/SSID auth on next attempt. Reverse with
    --unblock."""
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        resp = await client.get_devices(network_id=nid)
        devices = _extract_data_list(resp, "devices")
        match = _find_one_device(devices, args.device)
        if not match:
            return 1
        did = _device_id_from_url(match.get("url", ""))
        if not did:
            print(f"matched {_device_label(match)} but device has no URL/id; cannot mutate", file=sys.stderr)
            return 1
        try:
            await client.block_device(device_id=did, blocked=not args.unblock, network_id=nid)
        except EeroAPIException as e:
            print(f"eero refused: {e}", file=sys.stderr)
            return 3
        verb = "unblocked" if args.unblock else "blocked"
        print(f"{verb} {_device_label(match)} ({match.get('mac')})")
    return 0


async def _block_cleanup(args: argparse.Namespace) -> int:
    """Bulk-block (or --unblock) matching devices. Defaults to offline-only
    since blocking an online device boots it immediately, which is usually
    surprising. When --unblock is set, selects currently-blocked devices."""
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        resp = await client.get_devices(network_id=nid)
        devices = _extract_data_list(resp, "devices")
        pat = _compile_pattern(args.pattern)
        # For --unblock we want already-blocked devices; for block we want unblocked ones
        target_blocked = bool(args.unblock)
        candidates = [
            d for d in devices
            if (pat.search(d.get("nickname") or "") or pat.search(d.get("hostname") or ""))
            and (args.include_online or not d.get("connected"))
            and bool(d.get("blacklisted")) == target_blocked
        ]
        if not candidates:
            state = "already-blocked" if args.unblock else "unblocked"
            print(f"No {state} devices match /{args.pattern}/ (online filtered out unless --include-online)")
            return 0
        verb = "unblock" if args.unblock else "block"
        print(f"Will {verb} {len(candidates)} device(s):\n")
        _print_device_table(candidates)
        if not args.yes:
            print()
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 0
        ok = 0
        failed = 0
        for d in candidates:
            did = _device_id_from_url(d.get("url", ""))
            if not did:
                print(f"  fail: {_device_label(d)} {d.get('mac')} -> no device URL")
                failed += 1
                continue
            try:
                await client.block_device(device_id=did, blocked=not args.unblock, network_id=nid)
                ok += 1
            except EeroAPIException as e:
                print(f"  fail: {_device_label(d)} {d.get('mac')} -> {e}")
                failed += 1
        print(f"\n{ok} {verb}ed, {failed} failed")
        return 0 if failed == 0 else 4


async def _delete_one(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        resp = await client.get_devices(network_id=nid)
        devices = _extract_data_list(resp, "devices")
        match = _find_one_device(devices, args.device)
        if not match:
            return 1
        if match.get("connected") and not args.force:
            print(
                f"Device {_device_label(match)} ({match.get('mac')}) is online. "
                "Eero refuses to forget online devices. Disconnect it first, "
                "or pass --force to attempt anyway.",
                file=sys.stderr,
            )
            return 2
        did = _device_id_from_url(match.get("url", ""))
        if not did:
            print(f"matched {_device_label(match)} but device has no URL/id; cannot mutate", file=sys.stderr)
            return 1
        try:
            await _forget_device(client, nid, did)
        except EeroAPIException as e:
            print(f"eero refused: {e}", file=sys.stderr)
            return 3
        print(f"forgot {_device_label(match)} ({match.get('mac')})")
    return 0


async def _cleanup(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        resp = await client.get_devices(network_id=nid)
        devices = _extract_data_list(resp, "devices")
        pat = _compile_pattern(args.pattern)
        candidates = [
            d for d in devices
            if (pat.search(d.get("nickname") or "") or pat.search(d.get("hostname") or ""))
            and (args.include_online or not d.get("connected"))
        ]
        if not candidates:
            print(f"No devices match /{args.pattern}/ (offline only unless --include-online)")
            return 0
        print(f"Will forget {len(candidates)} device(s):\n")
        _print_device_table(candidates)
        if not args.yes:
            print()
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 0
        ok = 0
        failed = 0
        for d in candidates:
            did = _device_id_from_url(d.get("url", ""))
            label = _device_label(d)
            mac = d.get("mac")
            if not did:
                print(f"  failed: {label} {mac} -> no device URL")
                failed += 1
                continue
            if d.get("connected") and not args.force:
                print(f"  skip (online): {label} {mac}")
                continue
            try:
                await _forget_device(client, nid, did)
                print(f"  forgot: {label} {mac}")
                ok += 1
            except EeroAPIException as e:
                print(f"  failed: {label} {mac} -> {e}")
                failed += 1
        print(f"\n{ok} forgotten, {failed} failed")
        return 0 if failed == 0 else 4


async def _list_profiles(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id)
        resp = await client.get_profiles(network_id=nid, refresh_cache=True)
        profiles = _extract_data_list(resp, "profiles")
        if args.search:
            profiles = [p for p in profiles if _matches_terms(_profile_search_text(p), args.search)]
        if args.json:
            result = []
            for p in profiles:
                entry = dict(p)
                if args.devices:
                    pid = str(p.get("id") or _profile_id_from_url(p.get("url", "")))
                    detail = await client.get_profile_devices(profile_id=pid, network_id=nid)
                    entry["devices"] = _extract_data_list(detail, "devices")
                result.append(entry)
            _dump_json(result)
            return 0
        _print_profile_table(profiles)
        if args.devices:
            for p in profiles:
                pid = str(p.get("id") or _profile_id_from_url(p.get("url", "")))
                detail = await client.get_profile_devices(profile_id=pid, network_id=nid)
                devices = _extract_data_list(detail, "devices")
                print(f"\n{_profile_label(p)}:")
                _print_device_table(devices)
    return 0


async def _status(args: argparse.Namespace) -> int:
    """Report auth state, account identity, and visible networks. Read-only.

    Exit 0 when authenticated, 1 when not, so scripts can gate on it.
    """
    async with _client(args) as client:
        authed = client.is_authenticated
        account: dict[str, str] = {}
        networks: list[dict] = []
        if authed:
            with contextlib.suppress(EeroException):
                acct = await client.get_account()
                data = acct.get("data", {}) if isinstance(acct, dict) else {}
                account = _account_summary(data if isinstance(data, dict) else {})
            with contextlib.suppress(EeroException):
                networks = _extract_networks(await client.get_networks())
        if args.json:
            _dump_json({
                "authenticated": authed,
                "session_path": str(args.session_path),
                "account": account,
                "networks": networks,
            })
            return 0 if authed else 1
        print(f"{'authenticated:':<14}{authed}")
        print(f"{'session:':<14}{args.session_path}")
        for key in ("name", "email", "phone"):
            if account.get(key):
                print(f"{key + ':':<14}{account[key]}")
        if authed:
            print(f"{'networks:':<14}{len(networks)}")
            for n in networks:
                print(f"  - {n.get('name')} ({_network_id_of(n)})")
        else:
            print("(run `eero auth` to sign in)")
    return 0 if authed else 1


async def _rename_device(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        devices = _extract_data_list(await client.get_devices(network_id=nid), "devices")
        match = _find_one_device(devices, args.device)
        if not match:
            return 1
        did = _device_id_from_url(match.get("url", ""))
        if not did:
            print(f"matched {_device_label(match)} but device has no URL/id; cannot rename", file=sys.stderr)
            return 1
        print(f"Will rename {_device_label(match)} ({match.get('mac')}) -> {args.nickname}")
        if not args.yes:
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 0
        try:
            await client.set_device_nickname(device_id=did, nickname=args.nickname, network_id=nid)
        except EeroAPIException as e:
            print(f"eero refused: {e}", file=sys.stderr)
            return 3
        print(f"renamed {_device_label(match)} -> {args.nickname}")
    return 0


async def _assign_profile_device(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        profiles = _extract_data_list(await client.get_profiles(network_id=nid, refresh_cache=True), "profiles")
        profile = _find_one_profile(profiles, args.profile)
        if not profile:
            return 1
        profile_id = str(profile.get("id") or _profile_id_from_url(profile.get("url", "")))
        devices = _extract_data_list(await client.get_devices(network_id=nid, refresh_cache=True), "devices")
        matches = []
        for query in args.devices:
            match = _find_one_device(devices, query)
            if not match:
                return 1
            matches.append(match)

        existing = _device_urls(await client.get_profile_devices(profile_id=profile_id, network_id=nid))
        urls = list(existing)
        seen = set(existing)
        changed = []
        for device in matches:
            url = device.get("url")
            if not url:
                print(f"matched {_device_label(device)} but device has no URL; cannot assign", file=sys.stderr)
                return 1
            if args.remove:
                if url in seen:
                    urls = [u for u in urls if u != url]
                    seen.remove(url)
                    changed.append(device)
            elif url not in seen:
                urls.append(url)
                seen.add(url)
                changed.append(device)

        verb = "remove from" if args.remove else "assign to"
        if not changed:
            print(f"No changes needed for {_profile_label(profile)}")
            return 0
        print(f"Will {verb} profile {_profile_label(profile)}:")
        _print_device_table(changed)
        if not args.yes:
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 0

        await client.set_profile_devices(profile_id=profile_id, device_urls=urls, network_id=nid)
        print(f"updated {_profile_label(profile)}; {len(urls)} device(s) assigned")
    return 0


async def _create_profile(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        print(f"Will create profile: {args.name}")
        if not args.yes:
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 0
        resp = await client.create_profile(name=args.name, network_id=nid)
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        created = data if isinstance(data, dict) else {}
        pid = created.get("id") or _profile_id_from_url(created.get("url", ""))
        print(f"created {_profile_label(created) if created else args.name} ({pid or 'unknown id'})")
    return 0


async def _schedule_profile(args: argparse.Namespace) -> int:
    start = _validate_time(args.start)
    end = _validate_time(args.end)
    days = _parse_days(args.days)
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        profiles = _extract_data_list(await client.get_profiles(network_id=nid, refresh_cache=True), "profiles")
        profile = _find_one_profile(profiles, args.profile)
        if not profile:
            return 1
        profile_id = str(profile.get("id") or _profile_id_from_url(profile.get("url", "")))
        block = _bedtime_block(days, start, end)
        print(f"Will set bedtime block on {_profile_label(profile)}: {start}-{end} on {', '.join(days)}")
        if not args.yes:
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 0
        existing = _count_schedules(await _get_profile_data(client, nid, profile_id))
        await client.set_profile_schedule(profile_id=profile_id, time_blocks=[block], network_id=nid)
        if existing:
            print(f"replaced {existing} existing schedule(s)")
        print(f"schedule updated for {_profile_label(profile)}")
    return 0


async def _clear_profile_schedule(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        profiles = _extract_data_list(await client.get_profiles(network_id=nid, refresh_cache=True), "profiles")
        profile = _find_one_profile(profiles, args.profile)
        if not profile:
            return 1
        profile_id = str(profile.get("id") or _profile_id_from_url(profile.get("url", "")))
        print(f"Will clear schedule for {_profile_label(profile)}")
        if not args.yes:
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 0
        cleared = _count_schedules(await _get_profile_data(client, nid, profile_id))
        await client.clear_profile_schedule(profile_id=profile_id, network_id=nid)
        print(f"schedule cleared for {_profile_label(profile)} ({cleared} removed)")
    return 0


async def _block_profile_apps(args: argparse.Namespace) -> int:
    apps = [a.strip().lower() for a in args.applications if a.strip()]
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        profiles = _extract_data_list(await client.get_profiles(network_id=nid, refresh_cache=True), "profiles")
        profile = _find_one_profile(profiles, args.profile)
        if not profile:
            return 1
        profile_id = str(profile.get("id") or _profile_id_from_url(profile.get("url", "")))
        current_resp = await client.get_blocked_applications(profile_id=profile_id, network_id=nid)
        data = current_resp.get("data", {}) if isinstance(current_resp, dict) else {}
        current: list[Any] = []
        if isinstance(data, dict):
            current = data.get("blocked_applications") or data.get("premium_dns", {}).get("blocked_applications") or []
        current = [str(a).lower() for a in current] if isinstance(current, list) else []
        final = apps
        if args.append:
            final = sorted(set(current) | set(apps))
        print(f"Will set blocked apps for {_profile_label(profile)}: {', '.join(final) or '(none)'}")
        if not args.yes:
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 0
        await client.set_blocked_applications(profile_id=profile_id, applications=final, network_id=nid)
        print(f"blocked applications updated for {_profile_label(profile)}")
    return 0


async def _auth(args: argparse.Namespace) -> int:
    """Two modes:
      eero auth <identifier>            -> sends SMS/email, persists partial state
      eero auth --code <code>           -> completes verify against persisted state
      eero auth <identifier> --code <c> -> single-shot if you somehow have both
    Falls back to interactive input() when neither is given (works in a real TTY)."""
    _ensure_session_dir(args.session_path)
    async with EeroClient(
        cookie_file=str(args.session_path),
        use_keyring=False,
    ) as client:
        if args.code and not args.identifier:
            # verify-only path
            ok = await client.verify(args.code)
            if not ok:
                print("verify failed: eero rejected the code", file=sys.stderr)
                return 1
        else:
            identifier = args.identifier or input("eero login (phone or email): ").strip()
            login_ok = await client.login(identifier)
            if not login_ok:
                print(
                    f"login request failed for {identifier} (no user_token in response). "
                    "Double-check the identifier.",
                    file=sys.stderr,
                )
                return 1
            print(f"login request sent to {identifier}.")
            if args.code:
                ok = await client.verify(args.code)
                if not ok:
                    print("verify failed: eero rejected the code", file=sys.stderr)
                    return 1
            else:
                # WORKAROUND: eero-api FileStorage auto-clears any session whose
                # session_expiry is None when loading (auth_storage.py line ~136).
                # Between login and verify, expiry is legitimately None — but that
                # makes split-process auth impossible. Stamp a 30-minute placeholder
                # so the next `eero auth --code <c>` invocation can load the
                # pre-verify session_id and complete the verify call.
                creds = client._api.auth._credentials
                creds.session_expiry = datetime.now() + timedelta(minutes=30)
                await client._api.auth._save_credentials()
                print(f"\nPartial state saved to {args.session_path}.")
                print("When the SMS arrives (within 30 min), finish with:")
                print("  eero auth --code <CODE>")
                with contextlib.suppress(OSError):
                    os.chmod(args.session_path, 0o600)
                return 0
        with contextlib.suppress(OSError):
            os.chmod(args.session_path, 0o600)
        networks_resp = await client.get_networks()
        nets = _extract_networks(networks_resp)
        print(f"Authenticated. Session written to {args.session_path}")
        print(f"Networks visible: {len(nets)}")
        for n in nets:
            nid = n.get("id") or (n.get("url", "").rstrip("/").rsplit("/", 1)[-1] if n.get("url") else "?")
            print(f"  - {n.get('name')} ({nid})")
    return 0


def _client(args: argparse.Namespace) -> EeroClient:
    _ensure_session_dir(args.session_path)
    return EeroClient(
        cookie_file=str(args.session_path),
        use_keyring=False,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eero",
        description="Tiny CLI for the eero mesh API (auth, list devices, forget, bulk cleanup).",
    )
    p.add_argument(
        "--session-path",
        type=Path,
        default=DEFAULT_SESSION_PATH,
        help=f"Session JSON file (default: {DEFAULT_SESSION_PATH})",
    )
    p.add_argument(
        "--network-id",
        default=None,
        help="Eero network id (required for destructive commands on multi-network accounts; ignored otherwise).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser(
        "auth",
        help="SMS/email login. Two-step (non-interactive) supported: `eero auth <id>` then `eero auth --code <code>`.",
    )
    pa.add_argument("identifier", nargs="?", help="Phone (+15551234567) or email (prompted if omitted)")
    pa.add_argument("--code", help="Verification code (skip interactive prompt)")
    pa.set_defaults(func=_auth)

    pst = sub.add_parser("status", help="Show auth state, account identity, and visible networks (read-only).")
    pst.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    pst.set_defaults(func=_status)

    pd = sub.add_parser("devices", help="List devices on the network.")
    pd.add_argument("--filter", help="Regex matched against device name/host/manufacturer/MAC/IP/URL.")
    pd.add_argument("--search", help="Plain-text terms matched across device name/host/manufacturer/MAC/IP/URL.")
    pd.add_argument("--mac", help="MAC prefix filter, e.g. 'BC:24:11' or 'bc2411'.")
    pd.add_argument("--offline", action="store_true", help="Only offline devices.")
    pd.add_argument("--online", action="store_true", help="Only online devices.")
    pd.add_argument("--json", action="store_true", help="Emit the device list as JSON instead of a table.")
    pd.set_defaults(func=_list_devices)

    pp = sub.add_parser("profiles", help="List profiles and optional profile devices.")
    pp.add_argument("--search", help="Plain-text terms matched against profile name/id/url.")
    pp.add_argument("--devices", action="store_true", help="Also list devices assigned to each matching profile.")
    pp.add_argument("--json", action="store_true", help="Emit the profile list as JSON instead of a table.")
    pp.set_defaults(func=_list_profiles)

    prn = sub.add_parser("rename", help="Set a device nickname.")
    prn.add_argument("device", help="Device ID or MAC.")
    prn.add_argument("nickname", help="New nickname to display in the eero app.")
    prn.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    prn.set_defaults(func=_rename_device)

    pc2 = sub.add_parser("profile-create", help="Create a new profile.")
    pc2.add_argument("name", help="Profile name.")
    pc2.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    pc2.set_defaults(func=_create_profile)

    pa2 = sub.add_parser("profile-assign", help="Assign matching device(s) to a profile.")
    pa2.add_argument("profile", help="Profile name/id search, e.g. Johnny")
    pa2.add_argument("devices", nargs="+", help="Device searches, e.g. 'Samsung' 'Johnny PC'")
    pa2.add_argument("--remove", action="store_true", help="Remove matching device(s) from the profile instead.")
    pa2.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    pa2.set_defaults(func=_assign_profile_device)

    ps = sub.add_parser("profile-schedule", help="Set a profile bedtime block schedule.")
    ps.add_argument("profile", help="Profile name/id search.")
    ps.add_argument("--start", required=True, help="Block start time, 24-hour HH:MM.")
    ps.add_argument("--end", required=True, help="Block end time, 24-hour HH:MM.")
    ps.add_argument(
        "--days",
        default="all",
        help="all, weekdays, weekends, or comma-separated day names. Default: all.",
    )
    ps.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    ps.set_defaults(func=_schedule_profile)

    psc = sub.add_parser("profile-schedule-clear", help="Clear a profile schedule.")
    psc.add_argument("profile", help="Profile name/id search.")
    psc.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    psc.set_defaults(func=_clear_profile_schedule)

    pba = sub.add_parser("profile-block-apps", help="Set profile blocked applications, e.g. youtube.")
    pba.add_argument("profile", help="Profile name/id search.")
    pba.add_argument("applications", nargs="+", help="Application identifiers, e.g. youtube tiktok.")
    pba.add_argument("--append", action="store_true", help="Append to existing blocked apps instead of replacing them.")
    pba.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    pba.set_defaults(func=_block_profile_apps)

    px = sub.add_parser(
        "delete",
        help="(EXPERIMENTAL) Try the DELETE endpoint. Eero's REST API does not actually expose "
        "device deletion; this returns 404 on consumer accounts. Kept for API research.",
    )
    px.add_argument("device", help="Device ID (last URL segment) or MAC address.")
    px.add_argument("--force", action="store_true", help="Try the DELETE even if eero says online.")
    px.set_defaults(func=_delete_one)

    pb = sub.add_parser("block", help="Block a single device by ID/MAC (use --unblock to reverse).")
    pb.add_argument("device", help="Device ID or MAC.")
    pb.add_argument("--unblock", action="store_true", help="Unblock instead of blocking.")
    pb.set_defaults(func=_block)

    pbc = sub.add_parser(
        "block-cleanup",
        help="Bulk-block matching devices (offline-only default). Closest API-feasible alternative to forgetting.",
    )
    pbc.add_argument("pattern", help="Regex matched against nickname/hostname, e.g. '^s3-'")
    pbc.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    pbc.add_argument("--include-online", action="store_true", help="Also include online devices.")
    pbc.add_argument("--unblock", action="store_true", help="Unblock instead of blocking.")
    pbc.set_defaults(func=_block_cleanup)

    pc = sub.add_parser(
        "cleanup",
        help="(EXPERIMENTAL) Tries DELETE for each match. Same caveat as `delete`: does not "
        "actually work against eero's public REST API.",
    )
    pc.add_argument("pattern", help="Regex matched against nickname/hostname, e.g. '^s3-'")
    pc.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt.")
    pc.add_argument("--include-online", action="store_true", help="Also include online devices in match.")
    pc.add_argument("--force", action="store_true", help="Try DELETE even if a candidate is online.")
    pc.set_defaults(func=_cleanup)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130
    except EeroAuthenticationException as e:
        print(f"auth error: {e}\n(run `eero auth` to sign in)", file=sys.stderr)
        return 1
    except EeroAPIException as e:
        print(f"eero API error: {e}", file=sys.stderr)
        return 3
    except EeroException as e:
        print(f"eero client error: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"network/file error: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
