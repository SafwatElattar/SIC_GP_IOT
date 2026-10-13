import time, glob, threading
import board, digitalio
import RPi.GPIO as GPIO
from PIL import Image, ImageDraw, ImageFont
from adafruit_rgb_display import st7735
from max30102 import MAX30102
import hrcalc

LED_PIN = 22
TOUCH_PIN = 17
BUZZER_PIN = 27
BUZZER_ENABLED = False  # set to True after wiring the buzzer

GPIO.setmode(GPIO.BCM)
GPIO.setup(LED_PIN, GPIO.OUT)
GPIO.setup(TOUCH_PIN, GPIO.IN)
if BUZZER_ENABLED:
    GPIO.setup(BUZZER_PIN, GPIO.OUT)

# Display
cs = digitalio.DigitalInOut(board.CE0)
dc = digitalio.DigitalInOut(board.D24)
rst = digitalio.DigitalInOut(board.D25)
disp = st7735.ST7735R(board.SPI(), cs=cs, dc=dc, rst=rst,
                      width=128, height=160, rotation=0,
                      baudrate=2000000, bgr=True)


def load_font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


F_BIG, F_MID, F_SM = load_font(36), load_font(16), load_font(12)

state = {"bpm": 0, "spo2": 0, "finger": False, "temp": None}
bpm_hist, spo2_hist = [], []
beat_event = threading.Event()


def read_temp():
    try:
        f = glob.glob("/sys/bus/w1/devices/28-*/w1_slave")[0]
        lines = open(f).read().splitlines()
        if lines[0].endswith("YES"):
            return int(lines[1].split("t=")[1]) / 1000.0
    except Exception:
        pass
    return None


def median(lst):
    s = sorted(lst)
    return s[len(s) // 2]


def temp_thread():
    while True:
        state["temp"] = read_temp()
        time.sleep(1)


def hr_thread():
    m = MAX30102()
    red_buf, ir_buf = [], []
    fast = slow = None
    amp = 1.0
    armed = False
    last_beat = 0
    cnt = 0
    while True:
        # small batches (about 0.1 s) so the beat is detected close to real time
        red, ir = m.read_sequential(amount=10)
        red_buf = (red_buf + list(red))[-100:]
        ir_buf = (ir_buf + list(ir))[-100:]

        if sum(ir) / len(ir) < 50000:
            state["finger"] = False
            state["bpm"] = state["spo2"] = 0
            bpm_hist.clear()
            spo2_hist.clear()
            red_buf.clear()
            ir_buf.clear()
            fast = slow = None
            continue
        state["finger"] = True

        # beat detection on every sample
        for x in ir:
            if fast is None:
                fast = slow = x
            fast += 0.3 * (x - fast)
            slow += 0.02 * (x - slow)
            sig = fast - slow
            amp += 0.01 * (abs(sig) - amp)
            if sig > 0:
                armed = True
            elif armed and sig < -0.5 * amp:
                armed = False
                now = time.time()
                if now - last_beat > 0.35:  # max about 170 bpm
                    last_beat = now
                    beat_event.set()

        # BPM and SpO2 about once per second, smoothed with a median
        cnt += 1
        if cnt % 10 == 0 and len(ir_buf) == 100:
            bpm, bv, spo2, sv = hrcalc.calc_hr_and_spo2(ir_buf, red_buf)
            if bv and 40 < bpm < 180:
                bpm_hist.append(bpm)
                del bpm_hist[:-8]
                state["bpm"] = median(bpm_hist)
            if sv and 90 <= spo2 <= 100:
                spo2_hist.append(spo2)
                del spo2_hist[:-8]
                state["spo2"] = median(spo2_hist)


def led_thread():
    while True:
        beat_event.wait()
        beat_event.clear()
        if state["finger"]:
            GPIO.output(LED_PIN, 1)
            if BUZZER_ENABLED:
                GPIO.output(BUZZER_PIN, 1)
            time.sleep(0.08)
            GPIO.output(LED_PIN, 0)
            if BUZZER_ENABLED:
                GPIO.output(BUZZER_PIN, 0)


for t in (temp_thread, hr_thread, led_thread):
    threading.Thread(target=t, daemon=True).start()


# =====================================================================
#  MQTT  (ADDED BLOCK - nothing above or below this block was changed)
#  Publishes the values already stored in `state` to the PulseCare
#  dashboard using the same topic and JSON schema as the ESP32 example.
# =====================================================================
import json
import random
import paho.mqtt.client as mqtt

MQTT_BROKER = "broker.hivemq.com"      # same broker as the ESP32 example
MQTT_PORT = 1883
MQTT_TOPIC = "pulsecare/iot/current"   # topic watched by the dashboard
MQTT_INTERVAL = 2                      # seconds between publishes

_mqtt_id = "RPiPulseCare-%04x" % random.randint(0, 0xFFFF)
try:
    # paho-mqtt 2.x
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=_mqtt_id)
except AttributeError:
    # paho-mqtt 1.x
    mqtt_client = mqtt.Client(client_id=_mqtt_id)


def on_mqtt_connect(client, userdata, flags, rc, *extra):
    print("MQTT connected (rc=%s)" % rc)


def on_mqtt_disconnect(client, userdata, *args):
    print("MQTT disconnected, will retry automatically")


mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_disconnect = on_mqtt_disconnect
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=30)  # never blocks or crashes if offline
mqtt_client.loop_start()


def mqtt_thread():
    while True:
        time.sleep(MQTT_INTERVAL)
        finger = state["finger"]
        # finger is on but BPM / SpO2 are still being measured (about 10 s):
        # skip, so the dashboard does not see a false 0 BPM alarm
        if finger and (not state["bpm"] or not state["spo2"]):
            continue
        temp = state["temp"]
        payload = {
            "heart_rate": int(state["bpm"]) if finger else 0,
            "temperature": round(temp, 1) if temp is not None else None,
            "spo2": int(state["spo2"]) if finger else 0,
            "is_wearing": bool(finger),
            "timestamp": int(time.time() * 1000),   # epoch milliseconds
        }
        if mqtt_client.is_connected():
            mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=0)
            print("MQTT ->", payload)


threading.Thread(target=mqtt_thread, daemon=True).start()
# ===================== END OF ADDED MQTT BLOCK =======================


def center(d, y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text(((128 - w) / 2, y), text, font=font, fill=fill)


def draw(mode):
    img = Image.new("RGB", (128, 160), (0, 0, 0))
    d = ImageDraw.Draw(img)
    if mode == 0:
        center(d, 10, "TEMPERATURE", F_MID, (80, 255, 80))
        t = state["temp"]
        center(d, 60, "%.1f" % t if t is not None else "--", F_BIG, (255, 255, 255))
        center(d, 110, "deg C", F_MID, (80, 255, 80))
    elif mode == 1:
        center(d, 10, "HEART RATE", F_MID, (255, 80, 80))
        if state["finger"]:
            v = "%d" % state["bpm"] if state["bpm"] else "..."
        else:
            v = "--"
        center(d, 60, v, F_BIG, (255, 255, 255))
        center(d, 110, "BPM", F_MID, (255, 80, 80))
    else:
        center(d, 10, "OXYGEN", F_MID, (80, 200, 255))
        if state["finger"]:
            v = "%d" % state["spo2"] if state["spo2"] else "..."
        else:
            v = "--"
        center(d, 60, v, F_BIG, (255, 255, 255))
        center(d, 110, "SpO2 %", F_MID, (80, 200, 255))
    if mode != 0 and not state["finger"]:
        center(d, 140, "Place finger", F_SM, (150, 150, 150))
    # dots showing the current mode
    for i in range(3):
        c = (255, 255, 255) if i == mode else (70, 70, 70)
        d.ellipse((50 + i * 15, 152, 56 + i * 15, 158), fill=c)
    disp.image(img)


mode = 0
last_touch = 0
last_draw = 0
draw(mode)
try:
    while True:
        touch = GPIO.input(TOUCH_PIN)
        if touch and not last_touch:  # new touch only
            mode = (mode + 1) % 3
            draw(mode)
            last_draw = time.time()
        last_touch = touch
        if time.time() - last_draw > 0.5:
            draw(mode)
            last_draw = time.time()
        time.sleep(0.03)
except KeyboardInterrupt:
    GPIO.cleanup()