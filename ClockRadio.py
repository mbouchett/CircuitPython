# Clock Radio - Mark Bouchett 12/22/2025 Rev. 01/05/2025 00:05 Circuit Python
# Updated: Adds "snooze via mute" behavior:
#   - When alarm is ringing, pressing MUTE (hardware or browser) will snooze +9 minutes
#   - A second mute during the next ring snoozes +9 minutes again (max 2 snoozes)
#   - A third mute (during the 2nd snoozed ring) silences alarm for the rest of the day
#   - Turning Alarm Enable switch (GP19) OFF cancels any ringing/snooze immediately
#   - Resets automatically at the next day rollover

import busio
import board
from TEA5767 import Radio
import digitalio
import time
from adafruit_ht16k33.segments import Seg7x4
import os
import wifi
import socketpool
from adafruit_httpserver import Server, Request, Response, JSONResponse

# ----------------------------
# Init Controls & Devices
# ----------------------------
but_mute = digitalio.DigitalInOut(board.GP14)
but_mute.switch_to_input(pull=digitalio.Pull.UP)

stn_up = digitalio.DigitalInOut(board.GP15)
stn_up.switch_to_input(pull=digitalio.Pull.UP)

stn_down = digitalio.DigitalInOut(board.GP13)
stn_down.switch_to_input(pull=digitalio.Pull.UP)

but_hours = digitalio.DigitalInOut(board.GP16)
but_hours.switch_to_input(pull=digitalio.Pull.UP)

but_mins = digitalio.DigitalInOut(board.GP17)
but_mins.switch_to_input(pull=digitalio.Pull.UP)

sw_alarm = digitalio.DigitalInOut(board.GP18)
sw_alarm.switch_to_input(pull=digitalio.Pull.UP)

sw_alarm_enable = digitalio.DigitalInOut(board.GP19)
sw_alarm_enable.switch_to_input(pull=digitalio.Pull.UP)

# ----------------------------
# I2C + Radio + Display
# ----------------------------
i2c = busio.I2C(board.GP5, board.GP4, frequency=400000)

station = 99.9
mute = 0  # 0 = unmuted, 1 = muted
radio = Radio(i2c, freq=station)

display = Seg7x4(i2c, address=0x71)
display.brightness = 0.4
BRIGHT_DIM = 0.1
BRIGHT_ON  = 0.6

# ----------------------------
# Clock / Alarm Variables
# ----------------------------
SET_HOUR   = 12
SET_MINUTE = 0
SET_SECOND = 0
USE_12_HOUR = True
base_seconds = (SET_HOUR * 3600) + (SET_MINUTE * 60) + SET_SECOND

ALARM_HOUR   = 6
ALARM_MINUTE = 30
ALARM_SECOND = 0
alarm_seconds = (ALARM_HOUR * 3600) + (ALARM_MINUTE * 60) + ALARM_SECOND

start_monotonic = time.monotonic()

# ----------------------------
# Snooze State (NEW)
# ----------------------------
SNOOZE_SECONDS = 9 * 60
MAX_SNOOZES = 2

alarm_ringing = False          # True only while currently ringing
initial_fired_today = False    # True once the "main" alarm time has rung today
snooze_count = 0               # number of snoozes used today (0..2)
snooze_target = None           # seconds since midnight for next snoozed ring, or None
alarm_done_for_day = False     # True after user "dismisses" (third mute), prevents further rings

prev_current_seconds = None    # for day rollover detection

# ----------------------------
# WiFi + Web Server
# ----------------------------
ssid = os.getenv("CIRCUITPY_WIFI_SSID")
pw   = os.getenv("CIRCUITPY_WIFI_PASSWORD")

wifi.radio.connect(ssid, pw)
print("IP:", wifi.radio.ipv4_address)

pool = socketpool.SocketPool(wifi.radio)
server = Server(pool, "/")

# =========================================================
# Helpers
# =========================================================

def get_current_seconds():
    elapsed = int(time.monotonic() - start_monotonic)
    return (base_seconds + elapsed) % 86400

def set_clock(hh, mm, ss=0):
    global base_seconds, start_monotonic
    hh = int(hh) % 24
    mm = int(mm) % 60
    ss = int(ss) % 60
    base_seconds = (hh * 3600) + (mm * 60) + ss
    start_monotonic = time.monotonic()

def set_alarm(hh, mm, ss=0):
    global alarm_seconds
    hh = int(hh) % 24
    mm = int(mm) % 60
    ss = int(ss) % 60
    alarm_seconds = (hh * 3600) + (mm * 60) + ss

def increment_hour():
    global base_seconds, start_monotonic
    current = get_current_seconds()
    current = (current + 3600) % 86400
    base_seconds = current
    start_monotonic = time.monotonic()
    time.sleep(0.25)

def increment_minute():
    global base_seconds, start_monotonic
    current = get_current_seconds()
    current = (current + 60) % 86400
    base_seconds = current
    start_monotonic = time.monotonic()
    time.sleep(0.25)

def increment_alarm_hour():
    global alarm_seconds
    alarm_seconds = (alarm_seconds + 3600) % 86400
    time.sleep(0.25)

def increment_alarm_minute():
    global alarm_seconds
    alarm_seconds = (alarm_seconds + 60) % 86400
    time.sleep(0.25)

def apply_station(new_station):
    """Hard-set the radio to new_station MHz (most compatible approach)."""
    global radio, mute
    radio = Radio(i2c, freq=new_station)
    try:
        radio.mute(bool(mute))
    except Exception:
        pass
    display.print(f"{new_station:5.1f}")
    time.sleep(0.2)

def step_station(step):
    global station
    new_station = round(station + step, 1)
    if new_station < 88.0:
        new_station = 108.0
    elif new_station > 108.0:
        new_station = 88.0
    station = new_station
    apply_station(station)
    return station

def start_alarm_ring():
    """Force alarm audio ON (unmuted)."""
    global alarm_ringing, mute
    try:
        radio.mute(False)
    except Exception:
        pass
    mute = 0
    alarm_ringing = True

def stop_alarm_audio_mute():
    """Mute radio (used when snoozing/dismissing)."""
    global mute
    try:
        radio.mute(True)
    except Exception:
        pass
    mute = 1

def cancel_alarm_process_for_today():
    """Stop ringing/snoozes for the rest of the day."""
    global alarm_ringing, snooze_target, alarm_done_for_day
    alarm_ringing = False
    snooze_target = None
    alarm_done_for_day = True
    stop_alarm_audio_mute()

def reset_daily_alarm_state():
    """Called at midnight rollover."""
    global alarm_ringing, initial_fired_today, snooze_count, snooze_target, alarm_done_for_day
    alarm_ringing = False
    initial_fired_today = False
    snooze_count = 0
    snooze_target = None
    alarm_done_for_day = False

def alarm_disable_reset():
    """Called when alarm enable switch is OFF."""
    global alarm_ringing, initial_fired_today, snooze_count, snooze_target, alarm_done_for_day
    alarm_ringing = False
    initial_fired_today = False
    snooze_count = 0
    snooze_target = None
    alarm_done_for_day = False

def handle_mute_press():
    """
    If alarm is currently ringing:
      - 1st mute => snooze +9
      - 2nd mute => snooze +9 again
      - 3rd mute => dismiss for day
    Otherwise: normal mute toggle.
    """
    global alarm_ringing, snooze_count, snooze_target

    if alarm_ringing:
        # Snooze/dismiss behavior
        if snooze_count < MAX_SNOOZES:
            snooze_count += 1
            now = get_current_seconds()
            snooze_target = (now + SNOOZE_SECONDS) % 86400
            alarm_ringing = False
            stop_alarm_audio_mute()
            time.sleep(0.2)
            return
        else:
            # Already used 2 snoozes; third mute dismisses for day
            cancel_alarm_process_for_today()
            time.sleep(0.2)
            return

    # Normal toggle if not ringing
    global mute
    mute = 0 if mute else 1
    try:
        radio.mute(bool(mute))
    except Exception:
        pass
    time.sleep(0.2)

def update_clock():
    # If alarm switch is ON, show alarm time (24-hour)
    if sw_alarm.value == 0:
        hours = alarm_seconds // 3600
        minutes = (alarm_seconds % 3600) // 60
        display.print(f"{hours:02d}{minutes:02d}")
        display.colon = True
        return

    current = get_current_seconds()
    hours24 = current // 3600
    minutes = (current % 3600) // 60
    seconds = current % 60

    is_pm = hours24 >= 12

    if USE_12_HOUR:
        hours = hours24 % 12
        if hours == 0:
            hours = 12
    else:
        hours = hours24

    display.print(f"{hours:02d}{minutes:02d}")

    if USE_12_HOUR and is_pm:
        display.colon = True
    else:
        display.colon = (seconds % 2) == 0

# =========================================================
# Web UI
# =========================================================

@server.route("/", methods=("GET",))
def index(request: Request):
    html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Clock Radio</title>
  <style>
    body { font-family: sans-serif; margin: 16px; }
    .card { border: 1px solid #ccc; border-radius: 12px; padding: 12px; margin-bottom: 12px; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .btn { padding: 12px 14px; border-radius: 12px; border: 1px solid #333; background: #f4f4f4; font-size: 16px; }
    .btn-primary { background: #dff0ff; }
    .btn-danger { background: #ffdfe0; }
    input[type="time"] { font-size: 18px; padding: 10px; border-radius: 12px; border: 1px solid #aaa; }
    code { background: #f7f7f7; padding: 2px 6px; border-radius: 8px; }
    .big { font-size: 20px; font-weight: 700; }
    .muted { color: #555; }
    .freq { font-size: 22px; font-weight: 800; letter-spacing: 0.5px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="big">Clock Radio Control</div>
    <div class="muted">When alarm is ringing, Mute acts as Snooze (9 min, twice) then Dismiss.</div>
    <div style="margin-top:8px;">
      Current: <code id="cur">--:--</code> &nbsp; | &nbsp;
      Alarm: <code id="alm">--:--</code> &nbsp; | &nbsp;
      Armed (GP19): <code id="armed">?</code> &nbsp; | &nbsp;
      Mute: <code id="mute">?</code>
    </div>
    <div style="margin-top:8px;">
      Ringing: <code id="ring">?</code> &nbsp; | &nbsp;
      Snoozes used: <code id="sz">?</code>
    </div>
    <div style="margin-top:10px;">
      Station: <span class="freq" id="stn">--.-</span> <span class="muted">MHz</span>
    </div>
  </div>

  <div class="card">
    <div class="big">Radio</div>
    <div class="row" style="margin-top:8px;">
      <button id="muteBtn" class="btn btn-danger" onclick="hit('/mute_toggle')">Mute</button>
      <button class="btn" onclick="hit('/station_down')">-0.1</button>
      <button class="btn" onclick="hit('/station_up')">+0.1</button>
    </div>
  </div>

  <div class="card">
    <div class="big">Set Clock</div>
    <div class="row" style="margin-top:8px;">
      <input id="clockTime" type="time" step="60">
      <button class="btn btn-primary" onclick="setClock()">Set Clock</button>
      <button class="btn" onclick="hit('/clock_plus_hour')">+1 Hour</button>
      <button class="btn" onclick="hit('/clock_plus_min')">+1 Minute</button>
    </div>
  </div>

  <div class="card">
    <div class="big">Set Alarm</div>
    <div class="row" style="margin-top:8px;">
      <input id="alarmTime" type="time" step="60">
      <button class="btn btn-primary" onclick="setAlarm()">Set Alarm</button>
      <button class="btn" onclick="hit('/alarm_plus_hour')">+1 Hour</button>
      <button class="btn" onclick="hit('/alarm_plus_min')">+1 Minute</button>
    </div>
  </div>

<script>
function secToHHMM(s){
  s = s % 86400;
  const hh = String(Math.floor(s/3600)).padStart(2,'0');
  const mm = String(Math.floor((s%3600)/60)).padStart(2,'0');
  return hh + ":" + mm;
}

async function hit(path){
  try { await fetch(path); } catch(e) {}
  await refresh();
}

async function setClock(){
  const t = document.getElementById('clockTime').value;
  if(!t) return;
  const [hh, mm] = t.split(':');
  await hit(`/set_clock?hh=${hh}&mm=${mm}`);
}

async function setAlarm(){
  const t = document.getElementById('alarmTime').value;
  if(!t) return;
  const [hh, mm] = t.split(':');
  await hit(`/set_alarm?hh=${hh}&mm=${mm}`);
}

async function refresh(){
  try{
    const r = await fetch('/status');
    const j = await r.json();

    document.getElementById('cur').textContent = secToHHMM(j.current_seconds);
    document.getElementById('alm').textContent = secToHHMM(j.alarm_seconds);
    document.getElementById('armed').textContent = j.alarm_enabled ? "ON" : "OFF";

    const muted = j.mute_state ? true : false;
    document.getElementById('mute').textContent = muted ? "ON" : "OFF";
    document.getElementById('muteBtn').textContent = muted ? "Unmute" : "Mute";

    document.getElementById('ring').textContent = j.alarm_ringing ? "YES" : "NO";
    document.getElementById('sz').textContent = String(j.snooze_count);

    document.getElementById('stn').textContent = Number(j.station_mhz).toFixed(1);
  } catch(e) {}
}

setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""
    return Response(request, html, content_type="text/html")

# =========================================================
# Web API Routes
# =========================================================

@server.route("/status", methods=("GET",))
def status(request: Request):
    current = get_current_seconds()
    return JSONResponse(request, {
        "ip": str(wifi.radio.ipv4_address),
        "alarm_enabled": int(sw_alarm_enable.value == 0),
        "current_seconds": int(current),
        "alarm_seconds": int(alarm_seconds),
        "mute_state": int(mute),
        "station_mhz": float(station),
        "alarm_ringing": int(alarm_ringing),
        "snooze_count": int(snooze_count),
    })

@server.route("/mute_toggle", methods=("GET",))
def route_mute_toggle(request: Request):
    handle_mute_press()
    return Response(request, "OK", content_type="text/plain")

@server.route("/station_up", methods=("GET",))
def route_station_up(request: Request):
    step_station(+0.1)
    return Response(request, "OK", content_type="text/plain")

@server.route("/station_down", methods=("GET",))
def route_station_down(request: Request):
    step_station(-0.1)
    return Response(request, "OK", content_type="text/plain")

@server.route("/clock_plus_hour", methods=("GET",))
def route_clock_plus_hour(request: Request):
    increment_hour()
    return Response(request, "OK", content_type="text/plain")

@server.route("/clock_plus_min", methods=("GET",))
def route_clock_plus_min(request: Request):
    increment_minute()
    return Response(request, "OK", content_type="text/plain")

@server.route("/alarm_plus_hour", methods=("GET",))
def route_alarm_plus_hour(request: Request):
    increment_alarm_hour()
    return Response(request, "OK", content_type="text/plain")

@server.route("/alarm_plus_min", methods=("GET",))
def route_alarm_plus_min(request: Request):
    increment_alarm_minute()
    return Response(request, "OK", content_type="text/plain")

@server.route("/set_clock", methods=("GET",))
def route_set_clock(request: Request):
    try:
        hh = request.query_params.get("hh", None)
        mm = request.query_params.get("mm", None)
        ss = request.query_params.get("ss", 0)
        if hh is None or mm is None:
            return Response(request, "Missing hh or mm", content_type="text/plain", status=400)
        set_clock(int(hh), int(mm), int(ss))
        return Response(request, "OK", content_type="text/plain")
    except Exception:
        return Response(request, "Bad request", content_type="text/plain", status=400)

@server.route("/set_alarm", methods=("GET",))
def route_set_alarm(request: Request):
    try:
        hh = request.query_params.get("hh", None)
        mm = request.query_params.get("mm", None)
        ss = request.query_params.get("ss", 0)
        if hh is None or mm is None:
            return Response(request, "Missing hh or mm", content_type="text/plain", status=400)
        set_alarm(int(hh), int(mm), int(ss))
        return Response(request, "OK", content_type="text/plain")
    except Exception:
        return Response(request, "Bad request", content_type="text/plain", status=400)

# ----------------------------
# Start server
# ----------------------------
server.start(str(wifi.radio.ipv4_address), port=80)
print("Server started")

# =========================================================
# Main Loop
# =========================================================
while True:
    server.poll()

    # Current time and day rollover detection
    cur = get_current_seconds()
    if prev_current_seconds is None:
        prev_current_seconds = cur
    else:
        # If time wrapped around to a smaller number -> new "day"
        if cur < prev_current_seconds:
            reset_daily_alarm_state()
        prev_current_seconds = cur

    # Alarm enabled state from switch
    alarm_enabled = (sw_alarm_enable.value == 0)

    # Turning alarm OFF cancels ringing/snooze immediately
    if not alarm_enabled:
        alarm_disable_reset()

    # Brightness follows alarm enable switch
    display.brightness = BRIGHT_ON if alarm_enabled else BRIGHT_DIM

    # ----------------------------
    # Hardware buttons
    # ----------------------------
    if but_mute.value == 0:
        handle_mute_press()
        time.sleep(0.25)  # debounce

    if stn_up.value == 0:
        step_station(+0.1)
        time.sleep(0.25)

    if stn_down.value == 0:
        step_station(-0.1)
        time.sleep(0.25)

    # Clock/Alarm adjustment buttons depend on sw_alarm mode
    if sw_alarm.value == 0:
        if but_hours.value == 0:
            increment_alarm_hour()
        if but_mins.value == 0:
            increment_alarm_minute()
    else:
        if but_hours.value == 0:
            increment_hour()
        if but_mins.value == 0:
            increment_minute()

    # ----------------------------
    # Alarm + Snooze Logic (NEW)
    # ----------------------------
    if alarm_enabled and not alarm_done_for_day:
        cur_min = cur // 60
        alarm_min = alarm_seconds // 60

        # Fire initial alarm once per day
        if (not initial_fired_today) and (cur_min == alarm_min):
            initial_fired_today = True
            snooze_target = None
            start_alarm_ring()

        # Fire snoozed alarm (if scheduled)
        if (snooze_target is not None) and (cur_min == (snooze_target // 60)):
            snooze_target = None  # consume this target
            start_alarm_ring()

    # If alarm gets enabled later in the day, it should still work at the next matching time
    # (initial_fired_today will still be False until it rings or day rolls over)

    update_clock()
    time.sleep(0.5)

