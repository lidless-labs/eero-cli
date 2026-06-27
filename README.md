<p align="center">
  <img src="docs/assets/eero-cli-banner.jpg" alt="eero-cli banner" width="900">
</p>

<h1 align="center">eero-cli</h1>

<p align="center"><strong>Tiny terminal CLI for managing devices on an eero mesh network from a real shell.</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/version-0.1.0-blue?style=for-the-badge" alt="version 0.1.0">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="license MIT">
</p>

Wraps [`eero-api`](https://github.com/fulviofreitas/eero-api) (the most actively maintained reverse-engineered Python client as of May 2026) and adds:

- A two-step, **non-interactive** SMS auth flow that survives across separate shell invocations
- Filtered device listing (regex + MAC prefix + online/offline)
- Single and bulk device blocking
- An honest writeup, in this README, of what eero's API will and won't let you do — saving you the half-day I spent figuring it out

## Install

```bash
pipx install git+https://github.com/lidless-labs/eero-cli
# or, from a clone:
pipx install .
```

Requires Python 3.12+ (inherited from `eero-api`).

## First-time auth

The eero API uses a two-step SMS/email login. Both halves are non-interactive so they work in any wrapper that can't drive `input()` (Claude Code's `!` shell, scripted setup, etc.):

```bash
# Step 1: trigger the SMS / email
eero auth +15551234567        # phone
# or
eero auth you@example.com     # email
# Step 2 (after the code arrives, within 30 minutes):
eero auth --code 123456
```

Session token is written to `~/.config/eero/session.json` (mode 0600). Re-run `eero auth` any time to start over.

### Why two steps

The underlying `eero-api` library auto-clears any persisted session whose `session_expiry` is None when it loads — but `login()` legitimately leaves expiry None until `verify()` runs. Single-process auth works fine; cross-process auth would lose the partial state on read. `eero-cli` works around this by stamping a 30-minute placeholder expiry between the two calls.

## Commands

```bash
eero devices                      # list everything
eero devices --filter '^iphone'   # regex over nickname/hostname
eero devices --mac 'BC:24:11'     # MAC prefix
eero devices --offline            # only stale entries
eero devices --online             # only currently connected

eero block <mac-or-id>            # block a device (works on online devices)
eero block <mac-or-id> --unblock  # reverse it
eero block-cleanup '^bc24'        # bulk-block matching offline devices
eero block-cleanup '^bc24' -y     # skip confirmation prompt
```

`block-cleanup` defaults to offline-only and skips already-blocked devices. Pass `--include-online` to widen the net.

## What this CLI cannot do (and why)

The eero REST and GraphQL APIs **do not expose any mutating operation on offline devices**. We verified this end-to-end:

- `DELETE` on `/2.2/networks/<nid>/devices/<did>` returns `404` (also tested `/2.0/`, `/2.1/`, `/2.3/`, `/3.0/` API version prefixes; PATCH and OPTIONS too)
- `PUT` with `{"forget": true}` / `{"is_forgotten": true}` / `{"remove": true}` returns `200` but silently no-ops
- `POST` to `/devices/<id>/forget`, `/forget`, `/remove`, `/bulk_forget` all return `404`
- `block_device` against an offline device returns `200` but does not flip the `blacklisted`, `paused`, `dropped`, or any other state field
- The eero web admin SPA (`insight.eero.com`) ships **215 GraphQL mutations**; none match `*Forget*`, `*Delete*Device*`, or `*Remove*Device*` (only `BlockDevicesNetwork`, `EditDeviceNickname`, `EditDevicePaused`, `ToggleEeroBuiltinOnDevice`, `UnblockDevicesNetwork`, `UpdateDeviceSecondaryWan`)

The mobile app's "Forget Device" button must use either a private/internal channel or be local-only display state. No public reverse-engineered library — [`343max/eero-client`](https://github.com/343max/eero-client), [`fulviofreitas/eero-api`](https://github.com/fulviofreitas/eero-api), [`schmittx/home-assistant-eero`](https://github.com/schmittx/home-assistant-eero), or [`erikh/eero`](https://github.com/erikh/eero) — exposes device removal either.

**If you need to actually delete an offline device:**

1. Open the eero mobile app, tap the device, three dots → "Forget Device". Eero auto-culls truly stale entries after ~30 days, so doing nothing also works.
2. Or capture the real call via mitmproxy on your phone (left as an exercise; PR welcome if you find it).

## Underlying library

[`eero-api`](https://github.com/fulviofreitas/eero-api) by Fulvio Freitas — modern async client, version 4.1.3 at time of writing. Picked over the older `343max/eero-client` (last code push Feb 2024) because it's actively maintained, ships proper file-based credential storage, and has a clean async surface.

## License

MIT. See [LICENSE](./LICENSE).
