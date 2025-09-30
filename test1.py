#!/usr/bin/env python3
# Single-file Pi camera app: B/W capture + gallery + SSE + Waveshare ST7735 (rotated 90°)
# Adds a 3s on-LCD preview after each capture (scaled to fit).

import os
import json
import queue
from datetime import datetime
from pathlib import Path
from subprocess import run, CalledProcessError
from threading import Thread, Lock
from time import sleep

from flask import Flask, Response, jsonify, send_file, abort, render_template_string
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
PORT         = int(os.environ.get("PORT", "5050"))  # default to 5050 to avoid conflicts

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

def lcd_show_preview(pil_img, seconds=3.0):
    """
    Show a scaled, centered preview of pil_img on the LCD for 'seconds'.
    Keeps aspect ratio; letterboxes to 128x128 on black.
    """
    # Ensure RGB for the LCD
    im = pil_img.convert("RGB")
    # Create a copy to safely resize
    im = im.copy()
    # Fit within WIDTH x HEIGHT while preserving aspect ratio
    im.thumbnail((WIDTH, HEIGHT), Image.LANCZOS)
    # Center on a black canvas
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    x = (WIDTH - im.width) // 2
    y = (HEIGHT - im.height) // 2
    canvas.paste(im, (x, y))
    with lcd_lock:
        disp.display(canvas)
    sleep(seconds)

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

        # Save latest + timestamped
        img.save(LATEST_JPG, format="JPEG", quality=90)
        img.save(final_jpg, format="JPEG", quality=90)

        # Optional WEBP for faster preview (fallback to JPG if fails)
        try:
            img.save(LATEST_WEBP, format="WEBP", quality=90)
        except Exception:
            pass

        # ---- NEW: 3s preview on LCD (scaled fit) ----
        lcd_show_preview(img, seconds=3.0)
        # ---------------------------------------------

        # Back to Ready
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

# =============== Web app (inline HTML/CSS/JS) ====================
app = Flask(__name__)

INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Pi BnW Cam</title>
  <style>
    :root { --gap: 12px; color-scheme: light; }
    body{
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      font-weight:300;max-width:980px;margin:24px auto;padding:0 16px;
      background:#ECE7E1;color:#333
    }
    h2{margin:0 0 8px;color:#eb5d40}
    .toolbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:16px;margin:24px 0}
    button{
      padding:14px 20px;border:0;border-radius:0;font-weight:400;
      font-family:inherit;cursor:pointer;font-size:16px;display:flex;align-items:center;gap:8px;
      background:#eb5d40;color:#fff
    }
    #status{margin:4px 0 16px;opacity:.8;min-height:1.2em;color:#eb5d40}
    .hero{display:grid;grid-template-columns:1fr;gap:16px;align-items:start}
    .hero img{width:100%;height:auto;border-radius:12px;display:block;box-shadow:0 6px 18px rgba(235,93,64,.2)}
    .muted{opacity:.7;font-size:14px;color:#eb5d40}
    .section{margin-top:24px}
    .grid{display:grid;grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));gap: var(--gap);}
    .card{
      position:relative;overflow:visible;border-radius:12px;background:#ECE7E1;border:2px solid #eb5d40;
      box-shadow:0 1px 4px rgba(235,93,64,.2);
      transition:transform .15s ease, box-shadow .15s ease;display:flex;flex-direction:column
    }
    .card:hover{transform:translateY(-2px); box-shadow:0 6px 18px rgba(235,93,64,.3)}
    .thumb{width:100%;height:160px;object-fit:cover;display:block;filter:grayscale(100%) contrast(110%);border-radius:8px 8px 0 0}
    .meta{background:#eb5d40;color:#fff;font-size:12px;padding:8px;border-radius:0 0 8px 8px;margin-top:auto}
  </style>
</head>
<body>
  <div class="toolbar">
    <button id="captureBtn">Capture BnW</button>
    <span id="status" class="muted"></span>
  </div>

  <div class="hero">
    <div>
      <img id="preview" alt="Last capture will appear here" />
      <div class="muted">Latest image (auto-updates)</div>
    </div>
  </div>

  <div class="section">
    <h2>Local captures <span class="muted" id="countLocal"></span></h2>
    <div id="gridLocal" class="grid"></div>
  </div>

<script>
const btn = document.getElementById("captureBtn");
const statusEl = document.getElementById("status");
const img = document.getElementById("preview");
const gridLocal = document.getElementById("gridLocal");
const countLocal = document.getElementById("countLocal");

async function capture() {
  btn.disabled = true;
  btn.textContent = "Capturing…";
  statusEl.textContent = "Taking picture and converting…";
  try {
    const r = await fetch("/capture", { method: "POST" });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "Unknown error");
    img.src = data.url;
    statusEl.textContent = "Done.";
    await refreshGallery();
  } catch (e) {
    console.error(e);
    statusEl.textContent = e.message || "Failed to capture. Check server logs.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Capture BnW";
  }
}
btn.addEventListener("click", capture);

// SSE: captured -> refresh
try {
  const es = new EventSource("/events");
  es.onmessage = async (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "captured") {
        img.src = "/latest.webp?ts=" + msg.ts;
        await refreshGallery();
      }
    } catch {}
  };
} catch {}

function fmtBytes(n){
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024*1024) return (n/1024).toFixed(1) + " KB";
  return (n/1024/1024).toFixed(2) + " MB";
}

function renderLocal(items){
  gridLocal.innerHTML = "";
  countLocal.textContent = `(${items.length})`;
  for (const it of items){
    const dt = new Date(it.mtimeMs);
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <a href="${it.url}" target="_blank" rel="noopener">
        <img class="thumb" loading="lazy" src="${it.url}" alt="${it.name}" />
      </a>
      <div class="meta">${dt.toLocaleString()} · ${fmtBytes(it.size)}</div>
    `;
    gridLocal.appendChild(card);
  }
}

async function refreshGallery(){
  try{
    const r = await fetch("/gallery.json");
    const data = await r.json();
    if (!data.ok) throw new Error("Gallery failed");
    renderLocal(data.local || []);
  }catch(e){
    console.error(e);
    statusEl.textContent = "Failed to load gallery.";
  }
}

(async function init(){
  img.src = "/latest.webp?ts=" + Date.now();
  await refreshGallery();
})();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

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
    print(f"Serving on http://0.0.0.0:{PORT}")
    try:
        from waitress import serve as waitress_serve  # optional production WSGI
        waitress_serve(app, host="0.0.0.0", port=PORT)
    except Exception:
        app.run(host="0.0.0.0", port=PORT, debug=False)

if __name__ == "__main__":
    main()
