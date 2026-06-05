# Hue v2 Migration + Simulation Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the ADSL-monitoring script from the deprecated Philips Hue v1 API to the CLIP v2 API, and add a file-driven simulation harness so any line state can be forced on demand and watched on the real lights without disrupting the internet.

**Architecture:** Stay a single file (`Get_Vigor165_DSL_Status.py`). Introduce two logical seams: a `read_status()` input seam that returns a normalized state (and reads a sim file when `ADSL_SIM_FILE` is set), and a `HueClient` output seam that owns all bridge communication. Build the input seam first so the sim harness exists to visually verify every later change; then introduce `HueClient` as a v1 wrapper; then swap its internals to v2.

**Tech Stack:** Python 3 (3.9 on the Pi), `requests`, `snmpget` (net-snmp), systemd, Philips Hue CLIP v2 (HTTPS + `hue-application-key`).

**Testing approach (per spec non-goals — read this):** There is **no pytest suite** by design; the chosen test goal is manual visual confidence. Each task is verified with: (1) `python3 -m py_compile` as a syntax gate, (2) ad-hoc `python3 -c "..."` assertions for pure functions (no test files, no suite), and (3) the simulation harness for end-to-end visual checks against the real bridge. The physical modem-unplug is only a final sanity check.

**Spec:** `docs/superpowers/specs/2026-06-05-hue-v2-and-sim-harness-design.md`

---

## File Structure

- **Modify:** `Get_Vigor165_DSL_Status.py` — the entire refactor lands here (single-file design).
- **Modify:** `adsl_monitoring.conf` — add `HUE_SHOWTIME_DIM_INTERVAL`.
- **Modify:** `README.md` — document v2, the sim harness, and the new config key.
- **Modify:** `CLAUDE.md` — reflect `HueClient`, normalized `read_status()`, sim mode, v2 API.

No new files, no new dependencies (`requests` already present).

---

## Task 1: Input seam — normalized states + `read_status()` + sim support

Replace the hex-string-matching control flow with four normalized states and a single `read_status()` that reads either `snmpget` or a sim file. Hue calls stay on the existing v1 module functions for now, so after this task the **sim harness already works** (driving v1 lights) and can verify the state machine.

**Files:**
- Modify: `Get_Vigor165_DSL_Status.py`

- [ ] **Step 1: Add normalized state constants and rename the SNMP hex constants**

Replace the existing block:
```python
READY = "52 45 41 44 59"
TRAINING = "54 52 41 49 4E 49 4E 47"
SHOWTIME = "53 48 4F 57 54 49 4D 45"
```
with:
```python
# Raw adslAturCurrStatus values as returned by snmpget (Hex-STRING form).
HEX_SHOWTIME = "53 48 4F 57 54 49 4D 45"
HEX_TRAINING = "54 52 41 49 4E 49 4E 47"
HEX_READY    = "52 45 41 44 59"

# Normalized line states the main loop switches on.
STATE_UP       = "UP"        # SHOWTIME  -> green, dimming down
STATE_TRAINING = "TRAINING"  # TRAINING  -> blinking yellow
STATE_DOWN     = "DOWN"      # READY     -> blinking red
STATE_ERROR    = "ERROR"     # snmpget failed / modem unreachable -> solid red

# Seconds between green dim-down steps in the UP state (tunable for sim testing).
DIM_INTERVAL = float(os.environ.get("HUE_SHOWTIME_DIM_INTERVAL", "5"))
```

- [ ] **Step 2: Add the status parser and `read_status()` with the sim branch**

Add these functions near the top of the FUNCTIONS section (after the `logging.basicConfig(...)` call). `snmpget_cmd` is the existing module-level list built in the START section.
```python
# Maps raw snmpget output to a normalized state. stderr (modem unreachable)
# -> ERROR; an unrecognized-but-clean status -> None (ignore and re-poll, which
# matches the old behaviour of falling through all the while-loops).
def parse_snmp_status(stdout, stderr):
    if stderr:
        return STATE_ERROR
    if HEX_SHOWTIME in stdout:
        return STATE_UP
    if HEX_TRAINING in stdout:
        return STATE_TRAINING
    if HEX_READY in stdout:
        return STATE_DOWN
    return None

_SIM_WORDS = {"up": STATE_UP, "training": STATE_TRAINING,
              "down": STATE_DOWN, "error": STATE_ERROR}
_sim_last = None  # last valid sim state, so an empty/garbled file holds steady

def read_sim_status(path):
    global _sim_last
    try:
        with open(path) as f:
            word = f.read().strip().lower()
    except OSError:
        word = ""
    if word in _SIM_WORDS:
        _sim_last = _SIM_WORDS[word]
    elif word:
        logging.warning("Unknown sim status %r, holding %s", word, _sim_last)
    return _sim_last

# Single poll of the line state. delay sleeps first (preserving the old per-call
# pacing). With ADSL_SIM_FILE set, the modem is bypassed entirely.
def read_status(delay=0):
    time.sleep(delay)
    sim_file = os.environ.get("ADSL_SIM_FILE")
    if sim_file:
        return read_sim_status(sim_file)
    proc = subprocess.run(snmpget_cmd, capture_output=True)
    return parse_snmp_status(proc.stdout.decode(), proc.stderr.decode())
```

- [ ] **Step 3: Delete the old `get_adsl_status()` function**

Remove the entire `def get_adsl_status(delay): ...` function (its error-retry responsibility moves into the ERROR sub-loop in Step 4).

- [ ] **Step 4: Replace the main loop with the normalized state machine**

Replace everything from `# Main loop` / `while True:` to end of file with:
```python
logging.info("Started: bridge=%s group=%s snmp_target=%s sim=%s",
             HUE_BRIDGE_HOST, HUE_GROUP, SNMP_TARGET_HOST,
             os.environ.get("ADSL_SIM_FILE") or "off")

# Main loop
while True:
    state = read_status(1)

    # UP / SHOWTIME: green, slowly dimming to off (calm = healthy)
    showtime_start = 0
    green_count = 254
    lights_on(1)
    while state == STATE_UP:
        if showtime_start == 0:
            logging.info("Entering showtime status")
            set_colour("green")
            showtime_start = 1
        if green_count > 0:
            new_bri(green_count)
            green_count -= 1
            if green_count == 1:
                lights_off()
        time.sleep(DIM_INTERVAL)
        state = read_status(0)

    # TRAINING: blinking yellow
    training_start = 0
    while state == STATE_TRAINING:
        if training_start == 0:
            logging.info("Entering training status")
            set_colour("yellow")
            training_start = 1
            new_bri(254)
        toggle_lights()
        state = read_status(2)

    # DOWN / READY: blinking red
    ready_start = 0
    while state == STATE_DOWN:
        if ready_start == 0:
            logging.info("Entering ready status")
            ready_start = 1
            set_colour("red")
            new_bri(254)
            time.sleep(1)
        toggle_lights()
        state = read_status(0.5)

    # ERROR: solid red, retry until the modem answers
    error_start = 0
    while state == STATE_ERROR:
        if error_start == 0:
            logging.info("Entering error status")
            set_colour("red")
            lights_on(254)
            error_start = 1
        state = read_status(2)
```

- [ ] **Step 5: Syntax gate**

Run: `python3 -m py_compile Get_Vigor165_DSL_Status.py && echo OK`
Expected: `OK`

- [ ] **Step 6: Logic check on the parser (no test suite — inline assertions)**

Run:
```bash
python3 - <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("m", "Get_Vigor165_DSL_Status.py")
# stub the module-level startup (key/snmp) by only importing functions we need:
src = open("Get_Vigor165_DSL_Status.py").read()
ns = {}
# exec only up to the START section so module-level snmpget/key code doesn't run
head = src.split("# START #")[0]
exec(compile(head, "head", "exec"), ns)
assert ns["parse_snmp_status"]("xx 53 48 4F 57 54 49 4D 45 xx", "") == "UP"
assert ns["parse_snmp_status"]("54 52 41 49 4E 49 4E 47", "") == "TRAINING"
assert ns["parse_snmp_status"]("52 45 41 44 59", "") == "DOWN"
assert ns["parse_snmp_status"]("anything", "Timeout") == "ERROR"
assert ns["parse_snmp_status"]("00 00 00", "") is None
print("parser OK")
PY
```
Expected: `parser OK`

- [ ] **Step 7: Visual check via the sim harness (v1 lights)**

Run (stop the live service first so two clients don't fight the group):
```bash
sudo systemctl stop adsl_monitoring
export ADSL_SIM_FILE=/tmp/adsl_sim
export HUE_SHOWTIME_DIM_INTERVAL=0.1
export HUE_API_KEY_FILE="$(pwd)/Philips_Hue_API_Key.txt"
echo up > /tmp/adsl_sim
python3 Get_Vigor165_DSL_Status.py &
sleep 4 ; echo training > /tmp/adsl_sim
sleep 4 ; echo down     > /tmp/adsl_sim
sleep 4 ; echo error    > /tmp/adsl_sim
sleep 4 ; kill %1
```
Expected: green fades, then yellow blinks, then red blinks, then solid red. Logs show each "Entering ... status" line once per transition.

- [ ] **Step 8: Commit**

```bash
git add Get_Vigor165_DSL_Status.py
git commit -m "Add normalized status states and file-driven sim source"
```

---

## Task 2: Output seam — `HueClient` (v1 wrapper) with a percent API

Introduce `HueClient`, move all bridge communication into it, and switch the loop to its percent-based API. Still the v1 protocol — behaviour identical — but the loop no longer references the API version, and brightness is now a percentage (ready for v2).

**Files:**
- Modify: `Get_Vigor165_DSL_Status.py`
- Modify: `adsl_monitoring.conf`

- [ ] **Step 1: Add the `HueClient` class (v1) and delete the old Hue functions**

Delete these existing functions: `hue_request`, `lights_on`, `lights_off`, `set_colour`, `new_bri`, `toggle_lights`. Replace them with:
```python
class HueClient:
    # xy chromaticity coordinates for each status colour (shared by v1 and v2).
    COLORS = {
        "red":    (0.6750, 0.3220),
        "yellow": (0.4684, 0.4759),
        "green":  (0.2151, 0.7106),
    }

    def __init__(self, host, app_key, group_v1, retry_delay, timeout):
        self._base = f"http://{host}/api/{app_key}/groups/{group_v1}"
        self._headers = {"Accept": "application/json"}
        self._retry_delay = retry_delay
        self._timeout = timeout

    # Retry forever on any transport error (DNS / connection / timeout), so a
    # transient bridge or name-resolution hiccup retries instead of crashing.
    def _request(self, method, url, **kwargs):
        error_logged = False
        while True:
            try:
                return requests.request(method, url, timeout=self._timeout, **kwargs)
            except requests.exceptions.RequestException as e:
                if not error_logged:
                    logging.warning("Hue request error, retrying every %ss: %s",
                                    self._retry_delay, e)
                    error_logged = True
                time.sleep(self._retry_delay)

    @staticmethod
    def _to_bri(pct):
        return max(0, min(254, round(pct / 100 * 254)))

    def _put(self, data):
        self._request("PUT", self._base + "/action/", headers=self._headers, data=data)

    def on(self, pct):
        self._put(f'{{"on": true, "bri": {self._to_bri(pct)}, "transitiontime": 0}}')

    def off(self):
        self._put('{"on": false, "transitiontime": 0}')

    def set_color(self, name):
        x, y = self.COLORS[name]
        self._put(f'{{"xy": [{x}, {y}], "transitiontime": 0}}')

    def set_brightness(self, pct):
        self._put(f'{{"bri": {self._to_bri(pct)}, "transitiontime": 0}}')

    def is_on(self):
        resp = self._request("GET", self._base, headers=self._headers)
        return json.loads(resp.text)["state"]["any_on"]
```

- [ ] **Step 2: Add a module-level `blink()` helper**

Add after the class (the old `toggle_lights` behaviour, now expressed via `is_on`):
```python
def blink(hue):
    if hue.is_on():
        hue.off()
    else:
        hue.on(100)
```

- [ ] **Step 3: Construct the client in the START section**

After the snmpget-exists check and before the signal handlers, add:
```python
hue = HueClient(HUE_BRIDGE_HOST, API_KEY, HUE_GROUP, HUE_RETRY_DELAY, HUE_TIMEOUT)
```

- [ ] **Step 4: Update the `shutdown()` handler to use the client**

Replace the `requests.put(...)` call inside `shutdown()` with:
```python
    try:
        hue.off()
    except Exception as e:
        logging.warning("Could not turn lights off during shutdown: %s", e)
```
(The bare `except` is acceptable here: shutdown must never hang or raise.)

- [ ] **Step 5: Route the main loop through the client (percent brightness)**

Apply these exact replacements in the main loop:
- `lights_on(1)` → `hue.on(round(1 / 254 * 100, 2))`
- `set_colour("green")` → `hue.set_color("green")`
- `new_bri(green_count)` → `hue.set_brightness(green_count / 254 * 100)`
- `lights_off()` → `hue.off()`
- `set_colour("yellow")` → `hue.set_color("yellow")`
- `new_bri(254)` → `hue.set_brightness(100)` (both occurrences: TRAINING and DOWN)
- `toggle_lights()` → `blink(hue)` (both occurrences: TRAINING and DOWN)
- `set_colour("red")` → `hue.set_color("red")` (both occurrences: DOWN and ERROR)
- `lights_on(254)` → `hue.on(100)` (in ERROR)

- [ ] **Step 6: Add the config key**

In `adsl_monitoring.conf`, under the Philips Hue section, add:
```ini
# Seconds between green dim-down steps in the UP/SHOWTIME state.
HUE_SHOWTIME_DIM_INTERVAL=5
```

- [ ] **Step 7: Syntax gate**

Run: `python3 -m py_compile Get_Vigor165_DSL_Status.py && echo OK`
Expected: `OK`

- [ ] **Step 8: Logic check on brightness conversion**

Run:
```bash
python3 - <<'PY'
src = open("Get_Vigor165_DSL_Status.py").read()
ns = {}
exec(compile(src.split("# START #")[0], "head", "exec"), ns)
to_bri = ns["HueClient"]._to_bri
assert to_bri(0) == 0
assert to_bri(100) == 254
assert to_bri(50) == 127
print("brightness OK")
PY
```
Expected: `brightness OK`

- [ ] **Step 9: Visual check via the sim harness (still v1)**

Repeat Task 1 / Step 7. Expected: identical behaviour (green fade, yellow blink, red blink, solid red). Confirms the `HueClient` refactor changed nothing visible.

- [ ] **Step 10: Commit**

```bash
git add Get_Vigor165_DSL_Status.py adsl_monitoring.conf
git commit -m "Introduce HueClient (v1) with percent API and dim-interval config"
```

---

## Task 3: Swap `HueClient` internals to the Hue v2 (CLIP) API

Rewrite only the inside of `HueClient`. Public methods (`on`, `off`, `set_color`, `set_brightness`, `is_on`) keep their signatures, so the main loop is untouched.

**Files:**
- Modify: `Get_Vigor165_DSL_Status.py`

- [ ] **Step 1: Suppress the self-signed-cert warning (top of file)**

Add to the imports section:
```python
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
```

- [ ] **Step 2: Replace the body of `HueClient` with the v2 implementation**

Replace the entire `HueClient` class with:
```python
class HueClient:
    # xy chromaticity coordinates for each status colour.
    COLORS = {
        "red":    (0.6750, 0.3220),
        "yellow": (0.4684, 0.4759),
        "green":  (0.2151, 0.7106),
    }

    def __init__(self, host, app_key, group_v1, retry_delay, timeout):
        self._root = f"https://{host}/clip/v2/resource"
        self._headers = {"hue-application-key": app_key}
        self._retry_delay = retry_delay
        self._timeout = timeout
        self._group_id = self._resolve_group(group_v1)
        self._group_url = f"{self._root}/grouped_light/{self._group_id}"

    def _request(self, method, url, **kwargs):
        error_logged = False
        while True:
            try:
                resp = requests.request(method, url, headers=self._headers,
                                        verify=False, timeout=self._timeout, **kwargs)
                errors = resp.json().get("errors") if resp.content else None
                if errors:
                    logging.warning("Hue v2 API errors: %s", errors)
                return resp
            except requests.exceptions.RequestException as e:
                if not error_logged:
                    logging.warning("Hue request error, retrying every %ss: %s",
                                    self._retry_delay, e)
                    error_logged = True
                time.sleep(self._retry_delay)

    # Resolve the v1 integer group (e.g. 17) to its v2 grouped_light UUID by
    # matching id_v1 == "/groups/<n>". Looked up once at startup.
    def _resolve_group(self, group_v1):
        id_v1 = f"/groups/{group_v1}"
        for rtype in ("room", "zone"):
            for r in self._get(f"/{rtype}"):
                if r.get("id_v1") == id_v1:
                    for svc in r.get("services", []):
                        if svc.get("rtype") == "grouped_light":
                            logging.info("Resolved %s -> grouped_light %s", id_v1, svc["rid"])
                            return svc["rid"]
        for g in self._get("/grouped_light"):
            if g.get("id_v1") == id_v1:
                logging.info("Resolved %s -> grouped_light %s", id_v1, g["id"])
                return g["id"]
        logging.error("Could not resolve grouped_light for %s", id_v1)
        sys.exit(1)

    def _get(self, path):
        resp = self._request("GET", self._root + path)
        return resp.json().get("data", [])

    def _put(self, payload):
        self._request("PUT", self._group_url, json=payload)

    def on(self, pct):
        self._put({"on": {"on": True}, "dimming": {"brightness": pct},
                   "dynamics": {"duration": 0}})

    def off(self):
        self._put({"on": {"on": False}, "dynamics": {"duration": 0}})

    def set_color(self, name):
        x, y = self.COLORS[name]
        self._put({"color": {"xy": {"x": x, "y": y}}, "dynamics": {"duration": 0}})

    def set_brightness(self, pct):
        self._put({"dimming": {"brightness": pct}, "dynamics": {"duration": 0}})

    def is_on(self):
        data = self._get(f"/grouped_light/{self._group_id}")
        return bool(data and data[0]["on"]["on"])
```

- [ ] **Step 3: Syntax gate**

Run: `python3 -m py_compile Get_Vigor165_DSL_Status.py && echo OK`
Expected: `OK`

- [ ] **Step 4: Startup / group-resolution check against the real bridge**

Run (service stopped, sim file holding a steady state):
```bash
sudo systemctl stop adsl_monitoring
export ADSL_SIM_FILE=/tmp/adsl_sim
export HUE_API_KEY_FILE="$(pwd)/Philips_Hue_API_Key.txt"
echo up > /tmp/adsl_sim
timeout 5 python3 Get_Vigor165_DSL_Status.py 2>&1 | head -5
```
Expected: a `Resolved /groups/17 -> grouped_light <uuid>` log line, then `Entering showtime status`. No tracebacks.

- [ ] **Step 5: Negative check — unresolvable group exits cleanly**

Run:
```bash
HUE_GROUP=9999 ADSL_SIM_FILE=/tmp/adsl_sim HUE_API_KEY_FILE="$(pwd)/Philips_Hue_API_Key.txt" \
  python3 Get_Vigor165_DSL_Status.py ; echo "exit=$?"
```
Expected: `Could not resolve grouped_light for /groups/9999` and `exit=1`.

- [ ] **Step 6: Full visual check via the sim harness (now v2)**

Repeat Task 1 / Step 7 (with `HUE_SHOWTIME_DIM_INTERVAL=0.1`). Expected: green fades, yellow blinks, red blinks, solid red — now driven entirely through the v2 API. This is the migration's acceptance test.

- [ ] **Step 7: Commit**

```bash
git add Get_Vigor165_DSL_Status.py
git commit -m "Swap HueClient to the Hue v2 (CLIP) API"
```

---

## Task 4: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `README.md`**

- In the intro/"How it works", change "Hue API v1" to "Hue API v2 (CLIP)".
- In the config table, add a row:
  `| `HUE_SHOWTIME_DIM_INTERVAL` | `5` | Seconds between green dim-down steps (UP state) |`
- Add a new "## Testing (simulation harness)" section containing the exact sim
  commands from Task 1 / Step 7, explaining `ADSL_SIM_FILE` and the keywords
  `up` / `training` / `down` / `error`, and noting the service must be stopped first.
- In "## Notes", replace the v1-deprecation note with: "Uses the Philips Hue v2
  (CLIP) API over HTTPS with `verify=False` (trusted home LAN)."

- [ ] **Step 2: Update `CLAUDE.md`**

- "How it works": note the script now switches on normalized states
  (`STATE_UP`/`TRAINING`/`DOWN`/`ERROR`) from `read_status()`, and that all bridge
  I/O is in `HueClient` (v2 / CLIP, HTTPS, `hue-application-key`, group resolved
  from `id_v1` at startup, brightness in percent).
- "Key conventions / gotchas": add that `ADSL_SIM_FILE` switches `read_status()`
  to file input for testing, and `HUE_SHOWTIME_DIM_INTERVAL` tunes the dim speed.
- Update the "Two hosts" / API bullet to say v2 over HTTPS with skipped TLS verify.

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "Document Hue v2 migration and simulation harness"
```

---

## Task 5: Deploy to the Raspberry Pi and verify live

**Files:** none (deployment only). Pi: `pi@192.168.2.53`, passwordless SSH.

- [ ] **Step 1: Stage the updated files to the Pi**

```bash
scp Get_Vigor165_DSL_Status.py adsl_monitoring.conf pi@192.168.2.53:/tmp/
```

- [ ] **Step 2: Install them and restart the service**

```bash
ssh pi@192.168.2.53 '
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
Expected: `active`; logs show the `Resolved /groups/17 -> grouped_light ...` line and `Entering showtime status` (the live line is normally up).

- [ ] **Step 3: Verify the deployed file matches the repo (hash check)**

```bash
shasum -a 256 Get_Vigor165_DSL_Status.py
ssh pi@192.168.2.53 'sudo shasum -a 256 /usr/local/bin/Get_Vigor165_DSL_Status.py'
```
Expected: identical hashes.

- [ ] **Step 4: Confirm clean shutdown still works**

```bash
ssh pi@192.168.2.53 'sudo systemctl stop adsl_monitoring; journalctl -u adsl_monitoring -n 3 --no-pager -o cat; sudo systemctl start adsl_monitoring'
```
Expected: a `Received signal SIGTERM, shutting down - turning lights off.` line; service comes back `active`.

- [ ] **Step 5: Final commit (if any deploy notes/changes) and push**

```bash
git push
```
Expected: repo, GitHub, and Pi all consistent.

---

## Self-Review Notes

- **Spec coverage:** architecture/seams (T1, T2), v2 client incl. TLS/payloads/group
  resolution/percent/error[] (T3), sim harness + dim interval (T1, T2), config (T2),
  error handling & startup guards (T3 step 5), deployment/cutover/rollback (T5),
  acceptance criteria (exercised across T1 step7, T3 steps 4–6, T5). All covered.
- **No pytest suite** is intentional (spec non-goal); verification is py_compile +
  inline `python3 -c` + sim harness, as stated in the header.
- **Type/signature consistency:** `HueClient` public methods `on(pct)`, `off()`,
  `set_color(name)`, `set_brightness(pct)`, `is_on()` are identical in T2 and T3;
  the loop (T1) and `blink()` (T2) call only those.
