"""eero-cli command-line interface.

Wraps fulviofreitas/eero-api with two things it doesn't ship:
  1. A device-forget call (DELETE /networks/<nid>/devices/<did>) — eero's app
     uses this for the "Forget Device" button. Only works on offline devices.
  2. A bulk cleanup command for clearing stale entries by hostname/MAC pattern.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

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
        raise SystemExit(f"invalid regex {pattern!r}: {e}")


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
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
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


def _filter_devices(
    devices: list[dict[str, Any]],
    name_regex: str | None = None,
    mac_prefix: str | None = None,
    only_offline: bool = False,
    only_online: bool = False,
) -> list[dict[str, Any]]:
    result = devices
    if name_regex:
        pat = re.compile(name_regex, re.IGNORECASE)
        result = [
            d for d in result
            if pat.search(d.get("nickname") or "")
            or pat.search(d.get("hostname") or "")
        ]
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


async def _list_devices(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id)
        resp = await client.get_devices(network_id=nid)
        devices = resp.get("data", []) if isinstance(resp, dict) else resp
        if args.filter:
            _compile_pattern(args.filter)  # surface regex errors early
        filtered = _filter_devices(
            devices,
            name_regex=args.filter,
            mac_prefix=args.mac,
            only_offline=args.offline,
            only_online=args.online,
        )
        _print_device_table(filtered)
    return 0


async def _block(args: argparse.Namespace) -> int:
    """Block a single device by ID or MAC. Survives reboots; the eero will
    refuse to give the MAC an IP/SSID auth on next attempt. Reverse with
    --unblock."""
    async with _client(args) as client:
        nid = await _resolve_network_id(client, args.network_id, destructive=True)
        resp = await client.get_devices(network_id=nid)
        devices = resp.get("data", []) if isinstance(resp, dict) else resp
        target = args.device.lower()
        norm_target = target.replace(":", "").replace("-", "")
        match = None
        for d in devices:
            mac = (d.get("mac") or "").lower()
            mac_norm = mac.replace(":", "").replace("-", "")
            did = _device_id_from_url(d.get("url", ""))
            if did == args.device or mac == target or mac_norm == norm_target:
                match = d
                break
        if not match:
            print(f"No device matches '{args.device}'", file=sys.stderr)
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
        devices = resp.get("data", []) if isinstance(resp, dict) else resp
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
        devices = resp.get("data", []) if isinstance(resp, dict) else resp
        target = args.device.lower()
        norm_target = target.replace(":", "").replace("-", "")
        match = None
        for d in devices:
            mac = (d.get("mac") or "").lower()
            mac_norm = mac.replace(":", "").replace("-", "")
            did = _device_id_from_url(d.get("url", ""))
            if did == args.device or mac == target or mac_norm == norm_target:
                match = d
                break
        if not match:
            print(f"No device matches '{args.device}' (try `eero devices` to see IDs/MACs)", file=sys.stderr)
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
        devices = resp.get("data", []) if isinstance(resp, dict) else resp
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
                print(f"  eero auth --code <CODE>")
                try:
                    os.chmod(args.session_path, 0o600)
                except OSError:
                    pass
                return 0
        try:
            os.chmod(args.session_path, 0o600)
        except OSError:
            pass
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

    pd = sub.add_parser("devices", help="List devices on the network.")
    pd.add_argument("--filter", help="Regex matched against nickname/hostname (case-insensitive).")
    pd.add_argument("--mac", help="MAC prefix filter, e.g. 'BC:24:11' or 'bc2411'.")
    pd.add_argument("--offline", action="store_true", help="Only offline devices.")
    pd.add_argument("--online", action="store_true", help="Only online devices.")
    pd.set_defaults(func=_list_devices)

    px = sub.add_parser(
        "delete",
        help="(EXPERIMENTAL) Try the DELETE endpoint. Eero's REST API does not actually expose device deletion; this returns 404 on consumer accounts. Kept for API research.",
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
        help="(EXPERIMENTAL) Tries DELETE for each match. Same caveat as `delete` — does not actually work against eero's public REST API.",
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
