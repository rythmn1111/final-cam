#!/usr/bin/env python3
import os
import json
import queue
from datetime import datetime
from pathlib import Path
from subprocess import run, CalledProcessError
from threading import Thread, Lock
from time import sleep

from flask import Flask, Response, jsonify, render_template, send_file, abort
from gpiozero import Button
from PIL import Image, ImageDraw, ImageFont
from st7735 import ST7735

# ================== Config ==================
USER_HOME    = os.path.expanduser("~")
PHOTOS_DIR   = os.path.join(USER_HOME, "photos")
LATEST_JPG   = os.path.join(PHOTOS_DIR, "latest.jpg")
LATEST_WEBP  = os.path.join(PHOTOS_DIR, "latest.webp")
CAP_W, CAP_H = 1024, 1024                 # capture size (square)
BUTTON_BCM   = 13                         # joystick press pin
AUTOFOCUS    = True                       # set False if your module lacks AF

os.makedirs(PHOTOS_DIR, exist_ok=True)

# ===== LCD params (rotated 90°) =====
DC_PIN, RST_PIN, BL_PIN = 25, 27, 24
WIDTH, HEIGHT           = 128, 128
OFFSET_LEFT, OFFSET_TOP = 2, 3
SPI_HZ                  = 2_000_000
BGR, INVERT, ROTATION   = True, False, 90  # rotated 90°

disp = ST7735(
    port=0, cs=0, dc=DC_PIN, rst=RST_PIN, backlight=BL_PIN,
    width=WIDTH, height=HEIGHT, rotation=ROTATION,
    bgr=BGR, invert=INVERT, spi_speed_hz=SPI_HZ,
    offset_left=OFFSET_LEFT, offset_top=OFFSET_TOP
)
disp.begin()

# Fonts
try:
    FONT_BOLD = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    FONT      = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
except Exception:
    FONT_BOLD = ImageFont.load_default()
    FONT      = ImageFont.load_default()

lcd_lock = Lock()

def lcd_show_text(line1="Ready", line2="Press button / Web"):
    """Render two centered lines on the LCD."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    d = ImageDraw.Draw(img)
    w1, h1 = d.textsize(line1, font=FONT_BOLD)
    w2, h2 = d.textsize(line2, font=FONT)
    total_h = h1 + 6 + h2
    y1 = (HEIGHT - total_h) // 2
    y2 = y1 + h1 + 6
    x1 = (WIDTH - w1) // 2
    x2 = (WIDTH - w2) // 2
    d.text((x1, y1), line1, font=FONT_BOLD, fill=(255, 255, 255))
    d.text((x2, y2), line2, font=FONT, fill=(200, 200, 200))
    with lcd_lock:
        disp.display(img)

lcd_show_text("Ready", "Press button / Web")

# =============== Capture logic ===============
btn = Button(BUTTON_BCM, pull_up=True, bounce_time=0.15)

def _list_images_sorted():
    p = Path(PHOTOS_DIR)
    files = [*p.glob("*.jpg"), *p.glob("*.jpeg"), *p.glob("*.png"), *p.glob("*.webp")]
    files.sort(key=lambda x: x.stat().st_mtime)
    return files

def capture_once():
    tmp_path = "/tmp/shot.jpg"

    lcd_show_text("Capturing...", datetime.now().strftime("%H:%M:%S"))

    cmd = [
        "libcamera-jpeg", "-n",
        "--width", str(CAP_W), "--height", str(CAP_H),
        "-o", tmp_path
    ]
    if AUTOFOCUS:
        cmd.extend(["--autofocus-mode", "continuous"])

    try:
        run(cmd, check=True)

        # --- Convert to Black & White (grayscale) ---
        img = Image.open(tmp_path).convert("L")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_jpg = os.path.join(PHOTOS_DIR, f"{ts}.jpg")

        img.save(LATEST_JPG, format="JPEG", quality=90)
        img.save(final_jpg, format="JPEG", quality=90)

        try:
            img.save(LATEST_WEBP, format="WEBP", quality=90)
        except Exception:
            pass

        lcd_show_text("Saved", datetime.now().strftime("%H:%M:%S"))
        sleep(0.8)
        lcd_show_text("Ready", "Press button / Web")
        print(f"Captured {final_jpg}")

        _broadcast({"type": "captured", "ts": int(datetime.now().timestamp())})
        return True, final_jpg
    except CalledProcessError as e:
        lcd_show_text("Capture ERR", "See logs")
        sleep(1.2)
        lcd_show_text("Ready", "Press button / Web")
        print("Capture failed:", e)
        return False, str(e)

def button_worker():
    print("Button worker ready (press to capture).")
    while True:
        btn.wait_for_press()
        capture_once()
        sleep(0.2)

# =============== SSE (server-sent events) ===============
_subscribers = []

def _broadcast(obj):
    data = "data: " + json.dumps(obj) + "\n\n"
    dead = []
    for q in list(_subscribers):
        try:
            q.put_nowait(data)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass

def _event_stream():
    q = queue.Queue(maxsize=10)
    _subscribers.append(q)
    yield "data: " + json.dumps({"type": "hello", "ts": int(datetime.now().timestamp())}) + "\n\n"
    try:
        while True:
            try:
                chunk = q.get(timeout=15)
                yield chunk
            except queue.Empty:
                yield ": keep-alive\n\n"
    finally:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass

# =============== Web app ====================
app = Flask(__name__, static_folder="static", template_folder="templates")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/events")
def events():
    return Response(_event_stream(), mimetype="text/event-stream")

@app.route("/latest.webp")
def latest_webp():
    target = LATEST_WEBP if os.path.exists(LATEST_WEBP) else LATEST_JPG
    if not os.path.exists(target):
        abort(404)
    mt = "image/webp" if target.endswith(".webp") else "image/jpeg"
    return send_file(target, mimetype=mt, as_attachment=False)

@app.route("/latest.jpg")
def latest_jpg():
    if not os.path.exists(LATEST_JPG):
        abort(404)
    return send_file(LATEST_JPG, mimetype="image/jpeg", as_attachment=False)

@app.route("/img/<path:name>")
def serve_image(name):
    safe = os.path.basename(name)
    target = os.path.join(PHOTOS_DIR, safe)
    if not os.path.exists(target):
        abort(404)
    ext = safe.lower().rsplit(".", 1)[-1]
    mt = "image/jpeg" if ext in ("jpg", "jpeg") else ("image/webp" if ext == "webp" else "image/png")
    return send_file(target, mimetype=mt, as_attachment=False)

@app.route("/gallery.json")
def gallery():
    items = []
    for f in _list_images_sorted():
        st = f.stat()
        items.append({
            "name": f.name,
            "url": f"/img/{f.name}",
            "size": st.st_size,
        #  @app.route("/img/")
   "mtimeMs": int(st.st_mtime * 1000),
        })
    return jsonify({"ok": True, "local": items})

@app.route("/capture", methods=["POST"])
def capture():
    ok, info = capture_once()
    if ok:
        url = "/latest.webp?ts=" + str(int(datetime.now().timestamp()))
        return jsonify({"ok": True, "url": url})
    return jsonify({"ok": False, "error": info}), 500

def main():
    Thread(target=button_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)

if __name__ == "__main__":
    main()
