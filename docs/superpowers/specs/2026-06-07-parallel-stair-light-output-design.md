# Design: Parallel stair-light output

Date: 2026-06-07
Status: Approved (pending spec review)

## Goal

Mirror the ADSL line status onto an RGBW stair-light strip *in parallel* with the
existing Philips Hue group, so the same up/training/down/error states are shown on
both. The stair strip is driven through its existing HTTP control API
(`POST /api/ext`, see the Stair-Light-Project firmware).

Secondary, opt-in output: the Hue path remains the primary indicator and must be
completely unaffected. A missing, slow, or broken stair controller must never
stall or crash the monitor.

Non-goals: changing any Hue behaviour; changing the stair firmware; authentication
(the stair API is trusted-LAN, no auth); a held-red timeout on the strip (the
firmware has none and adding one is separate work).

## Background: the stair API

`POST http://<host>/api/ext` with form field `state`. Accepted values (confirmed
in `rgbw_stair_light.ino`): `red`, `red_blink`, `green_fade`, `yellow_blink`,
`clear`. Behaviour:
- `red` — solid red, **held until the next command**.
- `red_blink` — red blinking every 500 ms, held until the next command.
- `yellow_blink` — yellow (red+green) blinking every 500 ms, held until next command.
- `green_fade` — green dimming full→off over ~30 s, then **auto-clears** (override released).
- `clear` — LEDs off immediately, override released.

While any command is active the strip suppresses its motion detection; after
`green_fade` finishes or `clear`, normal stair behaviour resumes (daytime
automation / night mode). No auth. Host: `StairLight.local`.

## State mapping

The monitor's normalized states map one-to-one to stair commands. Each command is
sent **once per state transition** (on entry), never every poll.

| Monitor state | Hue (unchanged) | Stair command |
| --- | --- | --- |
| `STATE_UP` (SHOWTIME) | green, dimming | `green_fade` |
| `STATE_TRAINING` | blinking yellow | `yellow_blink` |
| `STATE_DOWN` (READY) | blinking red | `red_blink` |
| `STATE_ERROR` (modem unreachable) | solid red | `red` |

Design consequences of "send once on entry":
- **UP → `green_fade` once.** The strip fades over ~30 s and auto-clears itself, so
  the stairs return to normal lighting even while the line stays up — this satisfies
  the explicit requirement "green ≤ 30 s, then stairs back to normal." The monitor
  must NOT re-send `green_fade` while UP persists.
- **ERROR/DOWN → `red`/`red_blink` held.** During a sustained outage the strip stays
  red until the line changes state (an intentional ambient alert; motion lighting is
  suppressed for the duration). This is the accepted behaviour.
- `STATE_TRAINING` → `yellow_blink`, held until the next transition.
- The unknown/`None` poll result sends nothing (no transition handler runs), leaving
  the strip in its current command — consistent with the Hue side doing nothing.

## Architecture

Single file (`Get_Vigor165_DSL_Status.py`), mirroring the existing `HueClient`
output-seam pattern.

**`class StairClient`** — owns all stair-controller communication.
- `__init__(self, host, timeout)`: stores `host` and `timeout`. No I/O at
  construction. An empty/falsy `host` means the feature is disabled.
- `signal(self, command)`: if `host` is falsy, return immediately (no-op). Otherwise
  best-effort `requests.post(f"http://{host}/api/ext", data={"state": command},
  timeout=self._timeout)`. Catch `requests.exceptions.RequestException`, log a
  warning, and swallow it. **No retry loop** — a stair failure must not block the
  monitor loop (which drives the primary Hue output).

The monitor constructs one `stair` instance alongside `hue` and calls
`stair.signal(...)` from each state's entry block.

## Loop integration

In the main loop, each state already has a first-iteration guard (`*_start`
sentinel) where it sets the Hue colour once. Add the matching stair call there:
- UP block (`showtime_start == 0`): after `hue.set_color("green")`, `stair.signal("green_fade")`.
- TRAINING block (`training_start == 0`): after `hue.set_color("yellow")`, `stair.signal("yellow_blink")`.
- DOWN block (`ready_start == 0`): after `hue.set_color("red")`, `stair.signal("red_blink")`.
- ERROR block (`error_start == 0`): after `hue.set_color("red")`, `stair.signal("red")`.

No other loop changes. Hue control flow and timing are untouched.

## Configuration

`adsl_monitoring.conf` gains two keys (read in the CONFIGURATION block):

| Variable | Default | Meaning |
| --- | --- | --- |
| `STAIR_HOST` | `` (empty) | Stair controller hostname/IP. Empty disables the stair output entirely. |
| `STAIR_TIMEOUT` | `3` | Per-request POST timeout (s) for the stair API. |

Deployment value: `STAIR_HOST=StairLight.local`. Existing deployments without these
keys default to disabled, so the change is inert until configured.

## Lifecycle / error handling

- **Construction order:** `stair = None` sentinel, then signal handlers registered,
  then `hue = HueClient(...)` (does blocking I/O), then `stair = StairClient(...)`
  (no I/O). `shutdown()` guards both: `if stair is not None: stair.signal("clear")`
  after the Hue `try_off`, so a SIGTERM during startup is still safe.
- **Shutdown:** send `clear` (best-effort) so the strip is not left stuck in an
  override when the monitor stops — analogous to turning the Hue lamp off.
- **Isolation:** `signal()` never raises and never retries; the monitor's primary
  loop and Hue output are unaffected by stair-controller problems.

## Testing

The existing simulation harness covers this for free: with `STAIR_HOST` set and
`ADSL_SIM_FILE` driving states, forcing `up`/`training`/`down`/`error` drives BOTH
the Hue group and the stairs, so the strip can be visually verified against the real
controller. Offline: `signal()` with an empty host is a no-op (assert no HTTP call);
`signal()` builds the correct URL/payload.

## Acceptance criteria

1. With `STAIR_HOST=StairLight.local`, entering each state sends exactly one matching
   `/api/ext` command (`green_fade`/`yellow_blink`/`red_blink`/`red`), once per
   transition, confirmed visually on the strip via the sim harness.
2. `green_fade` is sent once on UP entry and not re-sent while UP persists; the stairs
   return to normal within ~30 s.
3. With `STAIR_HOST` empty/unset, the monitor behaves exactly as today (no stair I/O).
4. A stair-controller outage (host down) does not stall or crash the monitor or change
   the Hue behaviour; the failure is logged and swallowed.
5. `systemctl stop` sends `clear` to the strip and exits cleanly.
