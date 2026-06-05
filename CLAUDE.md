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
| `adsl_monitoring.conf` | `/etc/adsl_monitoring/` | Site config (`KEY=VALUE`), loaded by systemd as env vars. Not secret. |
| `Philips_Hue_API_Key.txt` | `/etc/adsl_monitoring/` | **Secret**, git-ignored. Path is set by `HUE_API_KEY_FILE` (absolute by default). |

## How it works

The script (`Get_Vigor165_DSL_Status.py`) is a single infinite loop, no classes/modules:

0. **Loads config** from environment variables (`HUE_BRIDGE_HOST`, `HUE_GROUP`, `SNMP_TARGET_HOST`, etc.), each with a built-in default matching the original hard-coded value — so it still runs with no env set. systemd supplies these via `EnvironmentFile=`; logging uses the `logging` module to stdout (journald captures it). `SIGTERM`/`SIGINT` are trapped (`shutdown()`) to turn the lights off best-effort and exit 0, so `systemctl stop` is clean rather than a kill mid-loop.
1. **Reads the Hue API key** from `HUE_API_KEY_FILE` (absolute path by default), `.strip()`-ed. No longer CWD-dependent.
2. **Polls the modem** by shelling out to `snmpget` (SNMP v1, community `public`) against `192.168.2.2`, OID `.1.3.6.1.2.1.10.94.1.1.3.1.6.4` (`adslAturCurrStatus`). The status is matched against hard-coded hex strings: `SHOWTIME` (line up), `TRAINING` (syncing), `READY` (down/retraining).
3. **Drives Hue group 17** via the bridge's local REST API (`http://{HUE_BRIDGE_IP}/api/{KEY}/groups/17/...`):
   - `SHOWTIME` → green, then slowly dims to off over ~254 polls (calm = healthy).
   - `TRAINING` → solid yellow, blinking (toggles each poll).
   - `READY` → solid red, blinking.
   - `snmpget` error (modem unreachable, e.g. rebooting) → red, retries every 2s until it responds.

State transitions are detected by `*_start` sentinel flags so the timestamped log line and color are set only on the *first* iteration of each state's `while` loop.

## Key conventions / gotchas

- **Two hosts are referenced and they differ:** the modem is an IP (`192.168.2.2`, SNMP); the Hue bridge is a *hostname* (`HUE_BRIDGE_IP = "PhilipsHueBridge"`, HTTP). The bridge name resolves via **mDNS** (`PhilipsHueBridge.local`, served by `avahi-daemon`) — it is not in `/etc/hosts`.
- **Resilience (added after a boot-time crash):** both layers now tolerate transient failures. SNMP errors retry in `get_adsl_status()`; all Hue HTTP calls go through `hue_request()`, which retries forever on any `requests` transport error (DNS/connection/timeout) instead of crashing. The unit also has `Restart=on-failure`/`RestartSec=10` and waits on `network-online.target avahi-daemon.service` so the bridge name resolves before the first request. The original crash (`[Errno -3] Temporary failure in name resolution` at boot, then dead for days) is addressed by these together.
- Tuning lives in `adsl_monitoring.conf` (env vars), **except** the three status hex strings (`READY`/`TRAINING`/`SHOWTIME`), which are protocol constants and stay in the script. No argument parsing.

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
