# Parallel Stair-Light Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror the ADSL line status onto an RGBW stair-light strip in parallel with the existing Hue group, via the strip's `POST /api/ext` HTTP API, as a secondary opt-in output that never affects the primary Hue path.

**Architecture:** Add a `StairClient` class (single file, mirroring the existing `HueClient` output seam). It is best-effort: one `/api/ext` command per state *transition*, short timeout, errors swallowed, no retry. The main loop calls it from each state's existing entry block. Enabled only when `STAIR_HOST` is configured.

**Tech Stack:** Python 3.9 / requests 2.25.1, the stair firmware's `/api/ext` API (states `red`/`red_blink`/`green_fade`/`yellow_blink`/`clear`).

**Testing approach (per project standing decision):** No pytest suite. Verify with `python3 -m py_compile`, inline `python3 -c` assertions (monkeypatching `requests.post` to check URL/payload and the disabled-host no-op without hitting the real strip), and the existing `ADSL_SIM_FILE` harness for the live visual check.

**Spec:** `docs/superpowers/specs/2026-06-07-parallel-stair-light-output-design.md`

---

## File Structure

- **Modify:** `Get_Vigor165_DSL_Status.py` — `StairClient` class, config vars, construction, shutdown `clear`, loop wiring.
- **Modify:** `adsl_monitoring.conf` — `STAIR_HOST`, `STAIR_TIMEOUT`.
- **Modify:** `README.md`, `CLAUDE.md` — document the secondary output.

No new files or dependencies.

---

## Task 1: `StairClient` seam, config, construction, shutdown

Add the secondary-output client and wire its lifecycle (NOT the per-state loop calls yet — that is Task 2).

**Files:**
- Modify: `Get_Vigor165_DSL_Status.py`

- [ ] **Step 1: Add config vars.** In the CONFIGURATION block, immediately after the `SNMP_OID = ...` line, add:
```python

# --- Stair light (secondary, optional output) ---
STAIR_HOST        = os.environ.get("STAIR_HOST", "")
STAIR_TIMEOUT     = float(os.environ.get("STAIR_TIMEOUT", "3"))
```

- [ ] **Step 2: Add the `StairClient` class.** Insert it immediately AFTER the `HueClient` class's final method and BEFORE the `def blink(hue):` helper:
```python
class StairClient:
    # Secondary, best-effort output: drives the stair-light strip via its HTTP
    # /api/ext control API. Never retries and never raises — a stair-controller
    # problem must not affect the primary Hue output or the monitor loop. An
    # empty host disables it entirely.
    def __init__(self, host, timeout):
        self._url = f"http://{host}/api/ext" if host else None
        self._timeout = timeout

    def signal(self, command):
        if self._url is None:
            return
        try:
            requests.post(self._url, data={"state": command}, timeout=self._timeout)
        except requests.exceptions.RequestException as e:
            logging.warning("Stair signal '%s' failed: %s", command, e)
```

- [ ] **Step 3: Add the `stair = None` sentinel and construct the client.** In the START section, the code currently reads:
```python
hue = None

# Register shutdown handlers before constructing the client: the v2 client's
# constructor performs network I/O (group resolution) that can block.
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

hue = HueClient(HUE_BRIDGE_HOST, API_KEY, HUE_GROUP, HUE_RETRY_DELAY, HUE_TIMEOUT)
```
Change it to (add `stair = None` next to `hue = None`, and construct `stair` after `hue`):
```python
hue = None
stair = None

# Register shutdown handlers before constructing the client: the v2 client's
# constructor performs network I/O (group resolution) that can block.
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

hue = HueClient(HUE_BRIDGE_HOST, API_KEY, HUE_GROUP, HUE_RETRY_DELAY, HUE_TIMEOUT)
stair = StairClient(STAIR_HOST, STAIR_TIMEOUT)
```

- [ ] **Step 4: Send `clear` to the strip on shutdown.** The `shutdown()` function currently reads:
```python
def shutdown(signum, frame):
    logging.info("Received signal %s, shutting down - turning lights off.", signal.Signals(signum).name)
    if hue is not None:
        hue.try_off(3)
    sys.exit(0)
```
Change it to:
```python
def shutdown(signum, frame):
    logging.info("Received signal %s, shutting down - turning lights off.", signal.Signals(signum).name)
    if hue is not None:
        hue.try_off(3)
    if stair is not None:
        stair.signal("clear")
    sys.exit(0)
```

- [ ] **Step 5: Syntax gate.**
Run: `python3 -m py_compile Get_Vigor165_DSL_Status.py && echo OK`
Expected: `OK`

- [ ] **Step 6: Offline behaviour check (monkeypatched — does NOT touch the real strip).**
Run:
```bash
python3 - <<'PY'
src = open("Get_Vigor165_DSL_Status.py").read()
ns = {}
exec(compile(src.split("# START #")[0], "head", "exec"), ns)
StairClient = ns["StairClient"]
import requests
calls = []
orig = requests.post
requests.post = lambda *a, **k: calls.append((a, k)) or type("R", (), {})()
try:
    # Disabled host -> no HTTP at all
    StairClient("", 3).signal("red")
    assert calls == [], "empty host must not POST"
    # Enabled host -> exactly one POST with the right url/payload/timeout
    StairClient("StairLight.local", 3).signal("yellow_blink")
    assert len(calls) == 1, calls
    args, kw = calls[0]
    assert args[0] == "http://StairLight.local/api/ext", args
    assert kw["data"] == {"state": "yellow_blink"}, kw
    assert kw["timeout"] == 3, kw
finally:
    requests.post = orig
print("stair seam OK")
PY
```
Expected: `stair seam OK`

- [ ] **Step 7: Commit.**
```bash
git add Get_Vigor165_DSL_Status.py
git commit -m "Add StairClient secondary output seam (config, construction, shutdown clear)"
```

---

## Task 2: Wire stair commands into the state transitions

Add exactly one `stair.signal(...)` per state, inside the existing first-iteration (`*_start == 0`) guard so it fires once per transition. The Hue control flow is unchanged.

**Files:**
- Modify: `Get_Vigor165_DSL_Status.py`

- [ ] **Step 1: UP / SHOWTIME → `green_fade`.** In the main loop, the UP entry guard reads:
```python
        if showtime_start == 0:
            logging.info("Entering showtime status")
            hue.set_color("green")
            showtime_start = 1
```
Change it to:
```python
        if showtime_start == 0:
            logging.info("Entering showtime status")
            hue.set_color("green")
            stair.signal("green_fade")
            showtime_start = 1
```

- [ ] **Step 2: TRAINING → `yellow_blink`.** The TRAINING entry guard reads:
```python
        if training_start == 0:
            logging.info("Entering training status")
            hue.set_color("yellow")
            training_start = 1
            hue.set_brightness(100)
```
Change it to:
```python
        if training_start == 0:
            logging.info("Entering training status")
            hue.set_color("yellow")
            stair.signal("yellow_blink")
            training_start = 1
            hue.set_brightness(100)
```

- [ ] **Step 3: DOWN / READY → `red_blink`.** The DOWN entry guard reads:
```python
        if ready_start == 0:
            logging.info("Entering ready status")
            ready_start = 1
            hue.set_color("red")
            hue.set_brightness(100)
            time.sleep(1)
```
Change it to:
```python
        if ready_start == 0:
            logging.info("Entering ready status")
            ready_start = 1
            hue.set_color("red")
            stair.signal("red_blink")
            hue.set_brightness(100)
            time.sleep(1)
```

- [ ] **Step 4: ERROR → `red`.** The ERROR entry guard reads:
```python
        if error_start == 0:
            logging.info("Entering error status")
            hue.set_color("red")
            hue.on(100)
            error_start = 1
```
Change it to:
```python
        if error_start == 0:
            logging.info("Entering error status")
            hue.set_color("red")
            hue.on(100)
            stair.signal("red")
            error_start = 1
```

- [ ] **Step 5: Syntax gate.**
Run: `python3 -m py_compile Get_Vigor165_DSL_Status.py && echo OK`
Expected: `OK`

- [ ] **Step 6: Confirm exactly the four expected signals are wired (plus the shutdown clear).**
Run: `grep -n 'stair.signal' Get_Vigor165_DSL_Status.py`
Expected: five lines — `green_fade`, `yellow_blink`, `red_blink`, `red`, and `clear` (the shutdown one from Task 1). No `green_fade`/`yellow_blink`/`red_blink`/`red` appears more than once.

- [ ] **Step 7: Commit.**
```bash
git add Get_Vigor165_DSL_Status.py
git commit -m "Wire stair-light commands into state transitions"
```

---

## Task 3: Config file value and documentation

**Files:**
- Modify: `adsl_monitoring.conf`
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the config keys.** At the end of `adsl_monitoring.conf`, add:
```ini

# --- Stair light (secondary, optional output) ---
# Hostname/IP of the RGBW stair-light controller. Leave empty to disable.
STAIR_HOST=StairLight.local
STAIR_TIMEOUT=3
```

- [ ] **Step 2: Document in `README.md`.** Add these two rows to the configuration variables table (after the `HUE_SHOWTIME_DIM_INTERVAL` row):
```
| `STAIR_HOST` | (empty) | Stair-light controller host. Empty disables the parallel stair output. |
| `STAIR_TIMEOUT` | `3` | Per-request timeout (s) for the stair `/api/ext` POST. |
```
Then add a new section after "## Testing (simulation harness)":
```markdown
## Parallel stair-light output (optional)

If `STAIR_HOST` is set, the same line status is mirrored onto an RGBW stair-light
strip via its `POST /api/ext` API, in parallel with the Hue group. One command is
sent per state change:

| State | Stair command |
| --- | --- |
| UP / SHOWTIME | `green_fade` (green fades over ~30 s, then the stairs return to normal) |
| TRAINING | `yellow_blink` |
| DOWN / READY | `red_blink` (held until the line changes) |
| ERROR | `red` (held until the line changes) |

This output is best-effort and secondary: a missing or unreachable stair controller
is logged and ignored, and never affects the Hue output. On shutdown the strip is
sent `clear` to release the override. Leave `STAIR_HOST` empty to disable.
```

- [ ] **Step 3: Document in `CLAUDE.md`.** In the "How it works" section, after the bullet describing `HueClient`, add:
```markdown
6. **Optional secondary output:** if `STAIR_HOST` is set, `StairClient` mirrors the
   state onto an RGBW stair strip via `POST /api/ext` (commands `green_fade` /
   `yellow_blink` / `red_blink` / `red`), one per state transition. Best-effort
   (short timeout, errors swallowed, no retry) so it never affects the Hue path;
   `clear` is sent on shutdown. Empty `STAIR_HOST` disables it.
```
And in the "Layout & deployment mapping" / conf row note, append: `Also STAIR_HOST/STAIR_TIMEOUT for the optional stair output.`

- [ ] **Step 4: Commit.**
```bash
git add adsl_monitoring.conf README.md CLAUDE.md
git commit -m "Document parallel stair-light output and add config keys"
```

---

## Task 4: Live verification and deploy

Verifies against the real stair controller (`StairLight.local`, reachable from the dev machine) and the real bridge, then deploys to the Pi. This step drives the real stairs and Hue lights, so it is done deliberately with the user watching.

**Files:** none (verification + deploy).

- [ ] **Step 1: Sim-harness run driving BOTH outputs (user watches).** With the live Pi service stopped, run locally:
```bash
cd "/Users/tstolt/Library/CloudStorage/OneDrive-Persönlich/Documents/Github/ADSL_Monitoring"
ssh <PI_USER>@<PI_HOST> 'sudo systemctl stop adsl_monitoring'
export ADSL_SIM_FILE=/tmp/adsl_sim
export HUE_SHOWTIME_DIM_INTERVAL=0.1
export HUE_API_KEY_FILE="$PWD/Philips_Hue_API_Key.txt"
export STAIR_HOST=StairLight.local
echo up > /tmp/adsl_sim
python3 Get_Vigor165_DSL_Status.py &  PID=$!
sleep 8; echo training > /tmp/adsl_sim
sleep 8; echo down     > /tmp/adsl_sim
sleep 8; echo error    > /tmp/adsl_sim
sleep 6; kill $PID
ssh <PI_USER>@<PI_HOST> 'sudo systemctl start adsl_monitoring'
```
Expected on the STRIP: green fade (then back to normal) → yellow blink → red blink → solid red. On Hue: green fade → yellow blink → red blink → solid red. Logs show no unhandled errors (a `Stair signal ... failed` warning would indicate a connectivity problem to investigate).

- [ ] **Step 2: Disabled-output sanity (no STAIR_HOST) — optional.** Confirm that without `STAIR_HOST` the script makes no stair calls (already covered by Task 1 Step 6 offline; skip if confident).

- [ ] **Step 3: Deploy script + conf to the Pi.**
```bash
scp Get_Vigor165_DSL_Status.py adsl_monitoring.conf <PI_USER>@<PI_HOST>:/tmp/
ssh <PI_USER>@<PI_HOST> '
  sudo install -m 755 -o root -g root /tmp/Get_Vigor165_DSL_Status.py /usr/local/bin/Get_Vigor165_DSL_Status.py
  sudo install -m 644 -o root -g root /tmp/adsl_monitoring.conf /etc/adsl_monitoring/adsl_monitoring.conf
  rm -f /tmp/Get_Vigor165_DSL_Status.py /tmp/adsl_monitoring.conf
  sudo systemctl daemon-reload
  sudo systemctl restart adsl_monitoring
  sleep 6
  systemctl is-active adsl_monitoring
  journalctl -u adsl_monitoring -n 6 --no-pager -o cat
'
```
Expected: `active`; logs show the resolved group UUID and `Entering showtime status` (the live line is normally up — so the strip will get one `green_fade` and then return to normal).

- [ ] **Step 4: Hash check.**
```bash
shasum -a 256 Get_Vigor165_DSL_Status.py
ssh <PI_USER>@<PI_HOST> 'sudo shasum -a 256 /usr/local/bin/Get_Vigor165_DSL_Status.py'
```
Expected: identical hashes.

- [ ] **Step 5: Push.**
```bash
git push origin <branch>
```

---

## Self-Review Notes

- **Spec coverage:** StairClient seam + best-effort/no-retry (T1 S2); opt-in `STAIR_HOST` / `STAIR_TIMEOUT` (T1 S1, T3 S1); construction order + None sentinel (T1 S3); shutdown `clear` (T1 S4); one-command-per-transition mapping green_fade/yellow_blink/red_blink/red (T2); send-green_fade-once (guaranteed by the `*_start` sentinel placement in T2 S1); disabled = inert (T1 S6); isolation from Hue (best-effort signal, T1 S2); docs (T3); live test via sim harness + deploy (T4). All spec sections covered.
- **No pytest** is intentional (project standing decision); offline verification is monkeypatch-based.
- **Signature consistency:** `StairClient(host, timeout)` and `signal(command)` are used identically in T1 (construction, shutdown) and T2 (loop). `stair` global matches the `hue` sentinel pattern.
- **Anchor uniqueness:** T2 S3/S4 both touch a `hue.set_color("red")` line, but each is shown inside its full distinct guard block (`ready_start` vs `error_start`) so placement is unambiguous.
