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

# adslAturCurrStatus values, as returned by snmpget in Hex-STRING form.
# These are protocol constants, not configuration.
READY    = "52 45 41 44 59"
TRAINING = "54 52 41 49 4E 49 4E 47"
SHOWTIME = "53 48 4F 57 54 49 4D 45"

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

# Wrapper around requests that retries forever on any transport error
# (connection refused, DNS failure, timeout). A transient bridge or name
# resolution hiccup should retry instead of crashing the process, mirroring
# the snmpget retry behaviour in get_adsl_status().
def hue_request(method, url, **kwargs):
    error_logged = False
    while True:
        try:
            return requests.request(method, url, timeout=HUE_TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as e:
            if not error_logged:
                logging.warning("Hue request error, retrying every %ss: %s", HUE_RETRY_DELAY, e)
                error_logged = True
            time.sleep(HUE_RETRY_DELAY)

# This will switch the lights on and set the brightness to bri
# The colour of the lights will be whatever the last colour of that light was
def lights_on(bri):
    hue_request("PUT", f"http://{HUE_BRIDGE_HOST}/api/{API_KEY}/groups/{HUE_GROUP}/action/", headers=HEADERS, data=f'{{"on": true, "bri": {bri}, "transitiontime": 0}}')

# This will switch the lights off
def lights_off():
    hue_request("PUT", f"http://{HUE_BRIDGE_HOST}/api/{API_KEY}/groups/{HUE_GROUP}/action/", headers=HEADERS, data=f'{{"on": false, "transitiontime": 0}}')

# This will only set the colour (red, yellow or green) fast, brightness will be unchanged
# That also means that if it is off, it will stay off
def set_colour(colour):
    if   colour == "red":    colour = '{ "xy": [0.6750, 0.3220], "transitiontime":0 } '
    elif colour == "yellow": colour = '{ "xy": [0.4684, 0.4759], "transitiontime":0 } '
    elif colour == "green":  colour = '{ "xy": [0.2151, 0.7106], "transitiontime":0 } '
    hue_request("PUT", f"http://{HUE_BRIDGE_HOST}/api/{API_KEY}/groups/{HUE_GROUP}/action/", headers=HEADERS, data=colour)

# This will set the brightness to the value provided
# The colour will stay unchanged
def new_bri(value):
    hue_request("PUT", f"http://{HUE_BRIDGE_HOST}/api/{API_KEY}/groups/{HUE_GROUP}/action/", headers=HEADERS, data=f'{{"bri": {value}, "transitiontime": 0}}')

# If lights are off turn them on, if they are on turn them off
def toggle_lights():
    # Find out whether any of the ADSL lights are on
    group = hue_request("GET", f"http://{HUE_BRIDGE_HOST}/api/{API_KEY}/groups/{HUE_GROUP}")
    if json.loads(group.text)['state']['any_on']:
        hue_request("PUT", f"http://{HUE_BRIDGE_HOST}/api/{API_KEY}/groups/{HUE_GROUP}/action/", headers=HEADERS, data='{"on":false, "bri": 0, "transitiontime": 0}')
    else:
        hue_request("PUT", f"http://{HUE_BRIDGE_HOST}/api/{API_KEY}/groups/{HUE_GROUP}/action/", headers=HEADERS, data='{"on":true, "bri": 254, "transitiontime": 0}')

def get_adsl_status(delay):
    time.sleep(delay)
    CMD = subprocess.run(snmpget_cmd, capture_output=True)
    # If we encounter an error from snmpget command, catch and retry after 2 seconds
    # Sometimes, if the router is not ready rebooting, it will take some time for the response
    error_start = 0
    while CMD.stderr.decode():
        if error_start == 0:
            logging.info("Entering error status")
            error_start = 1
            set_colour("red")
            lights_on(254)
        CMD = subprocess.run(snmpget_cmd, capture_output=True)
        time.sleep(2)
    # We have a successful result from snmpget command, output is returned
    return(CMD.stdout.decode())

# On SIGTERM (systemctl stop) / SIGINT (Ctrl-C), turn the lights off best-effort
# and exit cleanly instead of being killed mid-loop. Uses a short timeout and
# no retry so shutdown never hangs even if the bridge is unreachable.
def shutdown(signum, frame):
    logging.info("Received signal %s, shutting down - turning lights off.", signal.Signals(signum).name)
    try:
        requests.put(
            f"http://{HUE_BRIDGE_HOST}/api/{API_KEY}/groups/{HUE_GROUP}/action/",
            headers=HEADERS,
            data='{"on": false, "transitiontime": 0}',
            timeout=3,
        )
    except requests.exceptions.RequestException as e:
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

# Clean up the lights on stop/restart
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

# Construct the snmpget command
snmpget_cmd = [SNMP_GET_CMD, "-v", SNMP_VERSION, "-r", SNMP_RETRY_COUNT, "-c", SNMP_COMMUNITY, SNMP_TARGET_HOST, SNMP_OID]

logging.info("Started: bridge=%s group=%s snmp_target=%s", HUE_BRIDGE_HOST, HUE_GROUP, SNMP_TARGET_HOST)

# Main loop
while True:
    # Get the value of adslAturCurrStatus via SNMP
    STATUS = get_adsl_status(1)

    # If we find SHOWTIME in STATUS slowly dim the green light down and continue
    # to query status, every 5 seconds is fast enough
    showtime_start = 0
    green_count = 254
    lights_on(1)
    while SHOWTIME in STATUS:
        # Log only the first time in this loop
        if showtime_start == 0:
            logging.info("Entering showtime status")
            set_colour("green")
            showtime_start = 1
        if green_count > 0:
            new_bri(green_count)
            # decrement green_count by 1
            green_count -= 1
            if green_count == 1:
                lights_off()
        time.sleep(5)
        STATUS = get_adsl_status(0)

    # If we find TRAINING in STATUS
    training_start = 0
    while TRAINING in STATUS:
        # Log only the first time in this loop
        if training_start == 0:
            logging.info("Entering training status")
            set_colour("yellow")
            training_start = 1
            new_bri(254)
        toggle_lights()
        STATUS = get_adsl_status(2)

    # If we find READY in STATUS
    ready_start = 0
    while READY in STATUS:
        # Log only the first time in this loop
        if ready_start == 0:
            logging.info("Entering ready status")
            ready_start = 1
            set_colour("red")
            new_bri(254)
            time.sleep(1)
        toggle_lights()
        STATUS = get_adsl_status(0.5)
