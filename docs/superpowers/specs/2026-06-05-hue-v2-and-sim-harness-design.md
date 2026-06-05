# Design: Migrate to Hue API v2 + add a simulation test harness

Date: 2026-06-05
Status: Approved (pending spec review)

## Goal

Two related goals, served by one refactor:

1. **Future-proof the Hue integration** by moving from the deprecated Philips Hue
   v1 local API to the CLIP **v2** API. No deadline — pure future-proofing.
2. **Make the script testable for "live visual confidence"**: force any line
   state on demand and watch the real lights react, without disrupting the
   internet connection and without waiting on the modem.

Both fall out of introducing two seams in the script: a **Hue client** (output)
and a **status source** (input). The v2 swap is then contained to the client,
and the status source becomes the test harness. The same simulation run that
proves the harness also serves as the **acceptance test for the v2 migration**.

Non-goals: automated pytest suite (explicitly deferred — the chosen test goal is
manual visual confidence), supporting v1 and v2 simultaneously, splitting the
script into multiple files/modules, certificate pinning.

## Why not "unplug the modem"

Rejected as the primary test method: it disrupts real internet, is slow (resync
takes minutes), is non-deterministic (`TRAINING` is a transient the hardware
passes through on its own schedule, not on demand), and can't cleanly isolate
the `snmpget`-error path from `READY`. It is retained only as an occasional
end-to-end sanity check.

## Architecture

Stay a **single file** (`Get_Vigor165_DSL_Status.py`). The seams are logical
(a class + a function), not separate modules — this preserves the trivial
deployment (one file to `/usr/local/bin`, no package / `PYTHONPATH` / unit
change). Splitting into modules is a future option if the file grows; YAGNI now.

Internal structure:

1. **`class HueClient`** — wraps *all* bridge communication. The main loop calls
   its methods and never touches `requests` directly. All v1-vs-v2 detail lives
   here and nowhere else. The existing infinite-retry `hue_request` wrapper moves
   inside it.

   Public methods (the loop's vocabulary):
   - `on(brightness_pct)` — turn group on at a brightness percentage
   - `off()`
   - `set_color(name)` — `"red"` / `"yellow"` / `"green"`
   - `set_brightness(pct)`
   - `is_on()` — current group on/off state (replaces the v1 `any_on` read used
     by the blink/toggle logic)

2. **`read_status()`** — returns a **normalized state**, one of four constants:
   `UP` / `TRAINING` / `DOWN` / `ERROR`.
   - Normal mode: shells out to `snmpget`, maps the Hex-STRING output to one of
     the four constants (the existing `SHOWTIME`/`TRAINING`/`READY` hex strings
     and the snmpget-error case become this mapping).
   - Simulation mode (`ADSL_SIM_FILE` env var set): reads a keyword from that
     file instead. The modem and "a human typing into a file" become
     interchangeable inputs.

3. **Main loop** switches on the four normalized constants instead of matching
   raw hex strings inline. Readability win, and decouples the loop from both the
   SNMP wire format and the Hue API version.

State → light behaviour mapping (unchanged from today, just re-expressed):

| Normalized state | Lights |
| --- | --- |
| `UP` (SHOWTIME) | green, slowly dimming to off |
| `TRAINING` | blinking yellow |
| `DOWN` (READY) | blinking red |
| `ERROR` (snmpget fails) | solid red, retry until modem answers |

## The Hue v2 client

**Transport & auth.** Base URL `https://<bridge>/clip/v2/resource/...`, header
`hue-application-key: <key>`. The existing 40-char v1 key works unchanged as the
v2 application key — no re-pairing. `xy` color coordinates carry over unchanged
(same red/yellow/green values used today).

**TLS.** `verify=False` with the urllib3 `InsecureRequestWarning` suppressed
once. Standard for local Hue v2 on a trusted home LAN. Decision owned by the
user; trade-off (no LAN MITM protection) accepted as low-risk for a home
line-status light. Not exposed as a config knob. CA pinning noted as possible
future hardening but explicitly out of scope.

**Payload translation** (inside `HueClient`):

| Action | v1 (old) | v2 (new) |
| --- | --- | --- |
| On + brightness | `{"on":true,"bri":N}` | `{"on":{"on":true},"dimming":{"brightness":PCT}}` |
| Off | `{"on":false}` | `{"on":{"on":false}}` |
| Color | `{"xy":[x,y]}` | `{"color":{"xy":{"x":x,"y":y}}}` |
| Brightness only | `{"bri":N}` | `{"dimming":{"brightness":PCT}}` |
| Instant (no fade) | `transitiontime:0` | `dynamics:{"duration":0}` |

Control endpoint: `PUT /clip/v2/resource/grouped_light/<uuid>`.
State read (`is_on`): `GET` the same URL → `data[0].on.on`.

**Group resolution.** v2 uses UUIDs, not the integer `17`. At startup
`HueClient` resolves the group's `grouped_light` UUID by matching the v1 id
(`/groups/17`): locate the room/zone whose `id_v1 == "/groups/17"` and follow its
`services` to the `grouped_light` rid (fall back to a direct `grouped_light`
`id_v1` match if present). Resolve once, log the UUID. `HUE_GROUP=17` stays in
config, now interpreted as "the v1 group id to resolve." More robust than
hardcoding a UUID. Resolution failure (bad key, group not found) → log error and
`exit(1)`, mirroring the existing missing-key / no-snmpget guards.

**Brightness rescale (0–254 → 0–100%).** `HueClient.set_brightness(pct)` and
`on(pct)` take a percentage; conversion from any internal 0–254 value happens at
that boundary. The `UP`-state green dim-down keeps its **existing step loop**
(decrement from full to off), with each step converted to a percentage on send —
so the animation's step count and total duration are unchanged from today. (Some
adjacent steps map to the same percentage, which is harmless; timing is what
matters and it is preserved.)

**Dim cadence is configurable.** Today the dim-down takes ~21 minutes
(~254 steps × 5s), which is impractical to observe in a sim session. Introduce
`HUE_SHOWTIME_DIM_INTERVAL` (default `5`, i.e. today's per-step cadence, so
default duration is unchanged). In simulation, set it to e.g. `0.1` to watch the
full green fade in seconds.

## Simulation harness

Triggered by a single env var, `ADSL_SIM_FILE`:

- **Unset** (the systemd service): `read_status()` behaves exactly as today via
  `snmpget`. The service can never accidentally enter sim mode because this var
  is deliberately **not** in `adsl_monitoring.conf`.
- **Set** (manual run): `read_status()` reads a keyword from the file each poll —
  `up` / `training` / `down` / `error`. Empty file or unrecognized keyword →
  hold the current state (logged once), so an empty file is harmless.

Usage (the operator becomes the modem):

```bash
sudo systemctl stop adsl_monitoring          # avoid two clients fighting over group 17
export ADSL_SIM_FILE=/tmp/adsl_sim
export HUE_SHOWTIME_DIM_INTERVAL=0.1          # fast fade so the dim-down is watchable
export HUE_API_KEY_FILE=/path/to/Philips_Hue_API_Key.txt
python3 Get_Vigor165_DSL_Status.py &
echo up       > /tmp/adsl_sim                 # green fades down
echo training > /tmp/adsl_sim                 # yellow blinks
echo down     > /tmp/adsl_sim                 # red blinks
echo error    > /tmp/adsl_sim                 # error-state red
```

If all four states look correct, the v2 client is verified.

## Configuration changes

`adsl_monitoring.conf` (systemd `EnvironmentFile`) gains one key:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HUE_SHOWTIME_DIM_INTERVAL` | `5` | Seconds between green dim-down steps in the `UP` state |

`HUE_BRIDGE_HOST` is reused; the URL scheme becomes `https`. `ADSL_SIM_FILE` is a
manual-run-only env var, never placed in the service config. All other existing
keys (`HUE_GROUP`, `HUE_API_KEY_FILE`, `SNMP_*`, retry/timeout) are unchanged.

## Error handling

- The infinite-retry `hue_request` wrapper (transport errors: DNS / connection /
  timeout) moves into `HueClient`, behaviour unchanged.
- `HueClient` additionally inspects v2 responses for a JSON `errors[]` array and
  logs any entries.
- Startup guards retained and extended: missing key file → `exit(1)`; `snmpget`
  not on PATH → `exit(1)`; **group UUID resolution failure → `exit(1)`**.
- `SIGTERM`/`SIGINT` shutdown (lights off, clean exit) is retained; the lights-off
  call goes through the v2 client.

## Deployment, cutover, rollback

- **Deployment**: unchanged shape — single file to `/usr/local/bin`, plus the one
  new conf key. Service user, key file, and unit ordering all unchanged.
- **Cutover**: during development the live v1 service keeps running; the v2 build
  is tested via a manual sim run with the service stopped (so two clients don't
  fight over group 17). Then deploy the new file + `systemctl restart` to go live.
- **Rollback**: `git revert` + redeploy the single file.
- **Final check**: after cutover, the live modem (normally SHOWTIME) should show
  the green dim-down; optionally one physical-unplug sanity check.

## Acceptance criteria

1. With `ADSL_SIM_FILE` set, feeding `up`/`training`/`down`/`error` produces the
   correct light behaviour for each, against the real bridge, via the v2 client.
2. With a fast `HUE_SHOWTIME_DIM_INTERVAL`, the green dim-down is observable end
   to end in a sim session.
3. The systemd service (no sim var) runs against the real modem and behaves as it
   does today, now via the v2 API.
4. `systemctl stop` turns the lights off and exits cleanly.
5. Startup fails fast with a clear log message on bad key / missing snmpget /
   unresolvable group.
