# ADSL Monitoring

Turns a group of **Philips Hue** lights into a live status indicator for an
ADSL/VDSL internet line. It polls a **Draytek Vigor 165** modem over SNMP and
colours the lights to match the connection state — a glance at the lamp tells
you whether the line is up, syncing, or down.

Runs as a systemd service on a Raspberry Pi.

## How it works

```
Vigor 165 modem  --SNMP (snmpget)-->  this script  --HTTP (Hue API v1)-->  Hue bridge --> light group
```

Every second or so the script reads the modem's `adslAturCurrStatus` OID and
maps it to the lights:

| Line state | SNMP value | Lights |
| --- | --- | --- |
| **SHOWTIME** (connected) | `SHOWTIME` | Green, slowly dimming to off — calm = healthy |
| **TRAINING** (syncing) | `TRAINING` | Blinking yellow |
| **READY** (down / retraining) | `READY` | Blinking red |
| modem unreachable | `snmpget` errors | Solid red, retries until it answers |

## Requirements

On the Raspberry Pi (tested on Debian 11 "bullseye", Python 3.9):

```bash
sudo apt update
sudo apt install snmp python3-requests
```

- **`snmp`** provides the `snmpget` CLI the script shells out to.
- **`python3-requests`** is the Hue HTTP client.

For local development outside the Pi, `pip install -r requirements.txt` covers
the Python side (you still need the `snmp` package for `snmpget`).

## Files & install layout

The repo mirrors three deployment locations on the Pi. There is no build step —
copy each file to its path:

| Repo file | Install to | Notes |
| --- | --- | --- |
| `Get_Vigor165_DSL_Status.py` | `/usr/local/bin/` | The daemon (mode 755) |
| `adsl_monitoring.service` | `/etc/systemd/system/` | The systemd unit |
| `adsl_monitoring.conf` | `/etc/adsl_monitoring/` | Site config (env vars) |
| `Philips_Hue_API_Key.txt` | `/etc/adsl_monitoring/` | **Secret** — your Hue API key, *not* in git |

The service runs as the unprivileged user `adsl_monitor`; the key file should be
owned by and readable only by that user (`chmod 600`).

```bash
sudo install -m 755 Get_Vigor165_DSL_Status.py /usr/local/bin/
sudo install -m 644 adsl_monitoring.service     /etc/systemd/system/
sudo mkdir -p /etc/adsl_monitoring
sudo install -m 644 adsl_monitoring.conf        /etc/adsl_monitoring/
# Create the user and the key file (see Configuration below), then:
sudo systemctl daemon-reload
sudo systemctl enable --now adsl_monitoring
```

## Configuration

All site settings live in `adsl_monitoring.conf` as `KEY=VALUE` lines, loaded by
systemd via `EnvironmentFile=`. The script falls back to built-in defaults if a
value (or the whole file) is missing.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HUE_BRIDGE_HOST` | `PhilipsHueBridge` | Hue bridge hostname/IP (resolved via mDNS by default) |
| `HUE_GROUP` | `17` | Hue group ID to control |
| `HUE_API_KEY_FILE` | `/etc/adsl_monitoring/Philips_Hue_API_Key.txt` | Path to the API key file |
| `HUE_RETRY_DELAY` | `5` | Seconds between Hue retries after an error |
| `HUE_TIMEOUT` | `10` | Per-request Hue HTTP timeout (s) |
| `SNMP_TARGET_HOST` | `192.168.2.2` | Modem IP |
| `SNMP_COMMUNITY` | `public` | SNMP v1 community |
| `SNMP_VERSION` | `1` | SNMP version |
| `SNMP_OID` | `.1.3.6.1.2.1.10.94.1.1.3.1.6.4` | `adslAturCurrStatus` |

**The Hue API key** is the bridge "username" from the Hue v1 API. Generate one by
pressing the bridge's link button and POSTing to `http://<bridge>/api`, then save
the returned string into the key file:

```bash
echo -n '<your-40-char-key>' | sudo tee /etc/adsl_monitoring/Philips_Hue_API_Key.txt
sudo chown adsl_monitor:adsl_monitor /etc/adsl_monitoring/Philips_Hue_API_Key.txt
sudo chmod 600 /etc/adsl_monitoring/Philips_Hue_API_Key.txt
```

## Operating

```bash
sudo systemctl {status|restart|stop} adsl_monitoring
journalctl -u adsl_monitoring -f          # live logs (state changes are logged)

# Verify the modem responds to SNMP by hand:
snmpget -v 1 -r 0 -c public 192.168.2.2 .1.3.6.1.2.1.10.94.1.1.3.1.6.4
```

The service auto-restarts on failure and is ordered after the network and
`avahi-daemon` (mDNS) so the bridge name resolves before the first request. On
`systemctl stop` it turns the lights off and exits cleanly.

## Notes

- Uses the **Philips Hue v1 (local) API**, which is deprecated in favour of the
  HTTPS CLIP v2 API but still functional on current bridges.
- The status hex strings (`READY`/`TRAINING`/`SHOWTIME`) are protocol constants
  and live in the script, not the config.
