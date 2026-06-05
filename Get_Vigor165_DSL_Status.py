# ======= #
# IMPORTS #
# ======= #
import json
import logging
import os
import signal
import subprocess
import sys
import time
from shutil import which

import requests

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
HEADERS           = {"Accept": "application/json"}

SNMP_GET_CMD      = "snmpget"
SNMP_VERSION      = os.environ.get("SNMP_VERSION", "1")
SNMP_RETRY_COUNT  = os.environ.get("SNMP_RETRY_COUNT", "0")
SNMP_COMMUNITY    = os.environ.get("SNMP_COMMUNITY", "public")
SNMP_TARGET_HOST  = os.environ.get("SNMP_TARGET_HOST", "192.168.2.2")
SNMP_OID          = os.environ.get("SNMP_OID", ".1.3.6.1.2.1.10.94.1.1.3.1.6.4")

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
    try:
        hue.off()
    except Exception as e:
        logging.warning("Could not turn lights off during shutdown: %s", e)
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

hue = HueClient(HUE_BRIDGE_HOST, API_KEY, HUE_GROUP, HUE_RETRY_DELAY, HUE_TIMEOUT)

# Clean up the lights on stop/restart
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

# Construct the snmpget command
snmpget_cmd = [SNMP_GET_CMD, "-v", SNMP_VERSION, "-r", SNMP_RETRY_COUNT, "-c", SNMP_COMMUNITY, SNMP_TARGET_HOST, SNMP_OID]

logging.info("Started: bridge=%s group=%s snmp_target=%s sim=%s",
             HUE_BRIDGE_HOST, HUE_GROUP, SNMP_TARGET_HOST,
             os.environ.get("ADSL_SIM_FILE") or "off")

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
            error_start = 1
        state = read_status(2)
