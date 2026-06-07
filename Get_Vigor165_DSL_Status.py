# ======= #
# IMPORTS #
# ======= #
import logging
import os
import signal
import subprocess
import sys
import time
from shutil import which

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

#===============#
# CONFIGURATION #
#===============#
# All site-specific settings are read from the environment so they can be
# supplied via the systemd unit's EnvironmentFile (adsl_monitoring.conf)
# without editing this script. The defaults match the original hard-coded
# values so the script still runs if no environment is provided.
HUE_BRIDGE_HOST   = os.environ.get("HUE_BRIDGE_HOST", "PhilipsHueBridge")
HUE_GROUP         = os.environ.get("HUE_GROUP", "17")
HUE_API_KEY_FILE  = os.environ.get("HUE_API_KEY_FILE", "/etc/adsl_monitoring/Philips_Hue_API_Key.txt")
HUE_RETRY_DELAY   = float(os.environ.get("HUE_RETRY_DELAY", "5"))
HUE_TIMEOUT       = float(os.environ.get("HUE_TIMEOUT", "10"))

SNMP_GET_CMD      = "snmpget"
SNMP_VERSION      = os.environ.get("SNMP_VERSION", "1")
SNMP_RETRY_COUNT  = os.environ.get("SNMP_RETRY_COUNT", "0")
SNMP_COMMUNITY    = os.environ.get("SNMP_COMMUNITY", "public")
SNMP_TARGET_HOST  = os.environ.get("SNMP_TARGET_HOST", "192.168.2.2")
SNMP_OID          = os.environ.get("SNMP_OID", ".1.3.6.1.2.1.10.94.1.1.3.1.6.4")

# --- Stair light (secondary, optional output) ---
STAIR_HOST        = os.environ.get("STAIR_HOST", "")
STAIR_TIMEOUT     = float(os.environ.get("STAIR_TIMEOUT", "3"))

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

# Log to stdout; journald adds its own timestamp, but we include one too so
# manual runs are equally readable.
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

#===========#
# FUNCTIONS #
#===========#

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
                # Log any v2 API errors, but never let a non-JSON body on an
                # otherwise-successful response crash the retry loop.
                if resp.content:
                    try:
                        errors = resp.json().get("errors")
                        if errors:
                            logging.warning("Hue v2 API errors: %s", errors)
                    except ValueError:
                        pass
                return resp
            except requests.exceptions.RequestException as e:
                if not error_logged:
                    logging.warning("Hue request error, retrying every %ss: %s",
                                    self._retry_delay, e)
                    error_logged = True
                time.sleep(self._retry_delay)

    # Resolve the v1 integer group (e.g. 17) to its v2 grouped_light UUID by
    # matching id_v1 == "/groups/<n>". Looked up once at startup; blocks
    # (via _request's retry loop) until the bridge answers.
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

    # v2 rejects/clamps very low brightness; floor at 1% so the late dim-down
    # steps don't log spurious API errors. Going fully dark uses off(), not 0%.
    @staticmethod
    def _clamp_brightness(pct):
        return max(1.0, min(100.0, pct))

    def on(self, pct):
        self._put({"on": {"on": True}, "dimming": {"brightness": self._clamp_brightness(pct)},
                   "dynamics": {"duration": 0}})

    def off(self):
        self._put({"on": {"on": False}, "dynamics": {"duration": 0}})

    # Best-effort single attempt (hard timeout, no retry) for clean shutdown,
    # so systemctl stop never hangs even if the bridge is unreachable.
    def try_off(self, timeout):
        try:
            requests.put(self._group_url, headers=self._headers, verify=False,
                         json={"on": {"on": False}, "dynamics": {"duration": 0}},
                         timeout=timeout)
        except requests.exceptions.RequestException as e:
            logging.warning("Could not turn lights off during shutdown: %s", e)

    def set_color(self, name):
        x, y = self.COLORS[name]
        self._put({"color": {"xy": {"x": x, "y": y}}, "dynamics": {"duration": 0}})

    def set_brightness(self, pct):
        self._put({"dimming": {"brightness": self._clamp_brightness(pct)}, "dynamics": {"duration": 0}})

    def is_on(self):
        data = self._get(f"/grouped_light/{self._group_id}")
        if not data:
            return False
        return bool(data[0].get("on", {}).get("on"))

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
        # ValueError covers a malformed host/URL (requests 2.25.1 raises plain
        # ValueError from prepare_url, which is not a RequestException).
        try:
            resp = requests.post(self._url, data={"state": command}, timeout=self._timeout)
            resp.raise_for_status()
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.warning("Stair signal '%s' failed: %s", command, e)

def blink(hue):
    if hue.is_on():
        hue.off()
    else:
        hue.on(100)

# On SIGTERM (systemctl stop) / SIGINT (Ctrl-C), turn the lights off best-effort
# and exit cleanly instead of being killed mid-loop. Uses a short timeout and
# no retry so shutdown never hangs even if the bridge is unreachable.
def shutdown(signum, frame):
    logging.info("Received signal %s, shutting down - turning lights off.", signal.Signals(signum).name)
    if hue is not None:
        hue.try_off(3)
    if stair is not None:
        stair.signal("clear")
    sys.exit(0)

#==============================================================================#

#=======#
# START #
#=======#

# Fetch the Hue API key from the key file (absolute path by default, so it does
# not depend on the process working directory).
if os.path.exists(HUE_API_KEY_FILE):
    with open(HUE_API_KEY_FILE, 'r') as keyfile:
        API_KEY = keyfile.read().strip()
else:
    logging.error("Hue API key file '%s' does not exist!", HUE_API_KEY_FILE)
    sys.exit(1)

# Check whether the snmpget command is existing
if not which(SNMP_GET_CMD):
    logging.error("snmpget command not found!")
    sys.exit(1)

hue = None
stair = None

# Register shutdown handlers before constructing the client: the v2 client's
# constructor performs network I/O (group resolution) that can block.
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

hue = HueClient(HUE_BRIDGE_HOST, API_KEY, HUE_GROUP, HUE_RETRY_DELAY, HUE_TIMEOUT)
stair = StairClient(STAIR_HOST, STAIR_TIMEOUT)

# Construct the snmpget command
snmpget_cmd = [SNMP_GET_CMD, "-v", SNMP_VERSION, "-r", SNMP_RETRY_COUNT, "-c", SNMP_COMMUNITY, SNMP_TARGET_HOST, SNMP_OID]

logging.info("Started: bridge=%s group=%s snmp_target=%s sim=%s stair=%s",
             HUE_BRIDGE_HOST, HUE_GROUP, SNMP_TARGET_HOST,
             os.environ.get("ADSL_SIM_FILE") or "off",
             STAIR_HOST or "disabled")

# Main loop
while True:
    state = read_status(1)

    # UP / SHOWTIME: green, slowly dimming to off (calm = healthy)
    showtime_start = 0
    green_count = 254
    hue.on(round(1 / 254 * 100, 2))
    while state == STATE_UP:
        if showtime_start == 0:
            logging.info("Entering showtime status")
            hue.set_color("green")
            stair.signal("green_fade")
            showtime_start = 1
        if green_count > 0:
            hue.set_brightness(green_count / 254 * 100)
            green_count -= 1
            if green_count == 1:
                hue.off()
        time.sleep(DIM_INTERVAL)
        state = read_status(0)

    # TRAINING: blinking yellow
    training_start = 0
    while state == STATE_TRAINING:
        if training_start == 0:
            logging.info("Entering training status")
            hue.set_color("yellow")
            stair.signal("yellow_blink")
            training_start = 1
            hue.set_brightness(100)
        blink(hue)
        state = read_status(2)

    # DOWN / READY: blinking red
    ready_start = 0
    while state == STATE_DOWN:
        if ready_start == 0:
            logging.info("Entering ready status")
            ready_start = 1
            hue.set_color("red")
            stair.signal("red_blink")
            hue.set_brightness(100)
            time.sleep(1)
        blink(hue)
        state = read_status(0.5)

    # ERROR: solid red, retry until the modem answers
    error_start = 0
    while state == STATE_ERROR:
        if error_start == 0:
            logging.info("Entering error status")
            hue.set_color("red")
            hue.on(100)
            stair.signal("red")
            error_start = 1
        state = read_status(2)
