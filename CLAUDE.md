# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Python daemon that visualizes the line status of a **Draytek Vigor 165** ADSL/VDSL modem on a group of **Philips Hue** lights. It polls the modem over SNMP and drives the lights to reflect the connection state — so the physical lamp acts as a live status indicator for the internet line.

The code runs on a Raspberry Pi (`192.168.2.53`, user `pi`, passwordless SSH) as a **systemd service** named `adsl_monitoring`, under a dedicated unprivileged user `adsl_monitor`. This repo is the working copy of the three deployed files.

## Layout & deployment mapping

This repo mirrors files that live in three different places on the Pi. There is **no build step** — editing here, then copying each file back to its Pi path, is the deploy.

| Repo file | Pi path | Notes |
|---|---|---|
| `Get_Vigor165_DSL_Status.py` | `/usr/local/bin/` | The daemon. |
| `adsl_monitoring.service` | `/etc/systemd/system/` | The unit. `WorkingDirectory=/etc/adsl_monitoring`, `User=adsl_monitor`, `EnvironmentFile=` the conf below. |
| `adsl_monitoring.conf` | `/etc/adsl_monitoring/` | Site config (`KEY=VALUE`), loaded by systemd as env vars. Not secret. Includes `HUE_SHOWTIME_DIM_INTERVAL` (default `5` s). Also STAIR_HOST/STAIR_TIMEOUT for the optional stair output. |
| `Philips_Hue_API_Key.txt` | `/etc/adsl_monitoring/` | **Secret**, git-ignored. Path is set by `HUE_API_KEY_FILE` (absolute by default). |

## How it works

The script (`Get_Vigor165_DSL_Status.py`) is a single infinite loop with a `HueClient` class handling all bridge I/O:

0. **Loads config** from environment variables (`HUE_BRIDGE_HOST`, `HUE_GROUP`, `SNMP_TARGET_HOST`, etc.), each with a built-in default matching the original hard-coded value — so it still runs with no env set. systemd supplies these via `EnvironmentFile=`; logging uses the `logging` module to stdout (journald captures it). `SIGTERM`/`SIGINT` are trapped (`shutdown()`) to turn the lights off best-effort and exit 0, so `systemctl stop` is clean rather than a kill mid-loop.
1. **Reads the Hue API key** from `HUE_API_KEY_FILE` (absolute path by default), `.strip()`-ed. No longer CWD-dependent.
2. **Resolves the v2 group UUID** at startup: `HueClient.__init__` matches `id_v1 == "/groups/17"` across `room`, `zone`, and `grouped_light` resources via the v2 API and stores the UUID. Blocks (with retry) until the bridge answers.
3. **Polls the modem** by shelling out to `snmpget` (SNMP v1, community `public`) against `192.168.2.2`, OID `.1.3.6.1.2.1.10.94.1.1.3.1.6.4` (`adslAturCurrStatus`). The raw hex strings are matched inside `parse_snmp_status()` and mapped to normalized states returned by `read_status()`.
4. **Main loop switches on normalized states** from `read_status()`:
   - `STATE_UP` → green, slowly dims to off over ~254 steps (calm = healthy). Step interval controlled by `HUE_SHOWTIME_DIM_INTERVAL`.
   - `STATE_TRAINING` → solid yellow, blinking (toggles each poll).
   - `STATE_DOWN` → solid red, blinking.
   - `STATE_ERROR` (snmpget failed / modem unreachable) → solid red, retries every 2 s until it responds.
5. **All bridge I/O** is in `HueClient` (v2/CLIP, HTTPS, `hue-application-key` header, brightness as percentage 0–100, `verify=False` for the bridge's self-signed cert on a trusted home LAN).
6. **Optional secondary output:** if `STAIR_HOST` is set, `StairClient` mirrors the
   state onto an RGBW stair strip via `POST /api/ext` (commands `green_fade` /
   `yellow_blink` / `red_blink` / `red`), one per state transition. Best-effort
   (short timeout, errors swallowed, no retry) so it never affects the Hue path;
   `clear` is sent on shutdown. Empty `STAIR_HOST` disables it.

State transitions are detected by `*_start` sentinel flags so the timestamped log line and color are set only on the *first* iteration of each state's `while` loop.

## Key conventions / gotchas

- **Two hosts are referenced and they differ:** the modem is an IP (`192.168.2.2`, SNMP); the Hue bridge is a *hostname* (`HUE_BRIDGE_HOST = "PhilipsHueBridge"`, HTTPS). The bridge name resolves via **mDNS** (`PhilipsHueBridge.local`, served by `avahi-daemon`) — it is not in `/etc/hosts`.
- **Resilience (added after a boot-time crash):** both layers now tolerate transient failures. SNMP errors retry in `read_status()`; all Hue HTTPS calls go through `HueClient._request()`, which retries forever on any `requests` transport error (DNS/connection/timeout) instead of crashing. The unit also has `Restart=on-failure`/`RestartSec=10` and waits on `network-online.target avahi-daemon.service` so the bridge name resolves before the first request. The original crash (`[Errno -3] Temporary failure in name resolution` at boot, then dead for days) is addressed by these together.
- Tuning lives in `adsl_monitoring.conf` (env vars), **except** the three status hex strings (`READY`/`TRAINING`/`SHOWTIME`), which are protocol constants and stay in the script. No argument parsing.
- **`ADSL_SIM_FILE`** — if set, `read_status()` reads the line state from that file instead of calling `snmpget`. Write one of the keywords `up` / `training` / `down` / `error` into the file to drive the state; empty or unknown content holds the last valid state. Not in the service config — for manual/dev runs only. Stop the service first to avoid two clients fighting over the group.
- **`HUE_SHOWTIME_DIM_INTERVAL`** (default `5` s) — seconds between green dim-down brightness steps in the `STATE_UP` loop. Set to `0.1` when using `ADSL_SIM_FILE` to watch the full fade in seconds.

## Running / operating (on the Pi)

```bash
# Service control
sudo systemctl {status|restart|stop} adsl_monitoring
journalctl -u adsl_monitoring -f          # live logs (the script prints timestamped state changes)

# Reproduce a poll by hand (verify modem reachable / OID)
snmpget -v 1 -r 0 -c public 192.168.2.2 .1.3.6.1.2.1.10.94.1.1.3.1.6.4

# Run the script manually (uses built-in defaults unless you export the conf vars)
set -a; . /etc/adsl_monitoring/adsl_monitoring.conf; set +a
sudo -u adsl_monitor --preserve-env python3 /usr/local/bin/Get_Vigor165_DSL_Status.py
```

No tests or linters. Runtime deps: Python 3 with `requests` (`requirements.txt`; on the Pi via the `python3-requests` apt package) and the `snmpget` CLI from the `snmp` apt package on PATH. See `README.md` for full install/config.

## Secrets

`Philips_Hue_API_Key.txt` holds a live Hue bridge API key and is git-ignored. Never commit it. This repo lives in a OneDrive-synced folder, so treat the key as already exposed to cloud sync — rotate it on the bridge if that's a concern.
