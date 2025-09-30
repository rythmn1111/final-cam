#!/usr/bin/env python3
# Pi camera app (optimized): WebP-only (<=100 KB), grayscale, faster capture,
# LCD rotated 90°, 3s on-LCD preview (scaled), gallery, SSE updates.

import os
import io
import json
import math
import queue
from datetime import datetime
from pathlib import Path
from subprocess import run, CalledProcessError
from threading import Thread, Lock
from time import sleep

from flask import Flask, Response, jsonify, send_file, abort, render_template_string
from subprocess import PIPE
from gpiozero import Button
from PIL import Image, ImageDraw, ImageFont
from st7735 import ST7735

# ================== Config ==================
USER_HOME      = os.path.expanduser("~")
PHOTOS_DIR     = os.path.join(USER_HOME, "photos")
LATEST_WEBP    = os.path.join(PHOTOS_DIR, "latest.webp")

# Resolution (keep 1024 if you like; dropping to ~900 can shave more time)
CAP_W, CAP_H   = 1024, 1024

BUTTON_BCM     = 13                  # joystick press pin
# BIG speed win: disable AF (continuous AF can stall seconds)
AUTOFOCUS      = False               # set True only if you need AF and your module supports it

PORT           = int(os.environ.get("PORT", "5050"))
ARWEAVE_JSON   = os.path.join(PHOTOS_DIR, "arweave.json")
MAX_BYTES      = 100 * 1024          # 100 KB hard cap per saved image

# WebP encoder tuning (lighter for speed)
Q_MIN, Q_MAX   = 30, 92              # slightly lower max to avoid wasteful tries
MIN_SIDE_PX    = 640                 # do not shrink below this shorter side
WEBP_METHOD    = 4                   # 0-6; 4 is much faster than 6 with small quality tradeoff
MAX_DOWNSCALE_STEPS = 5              # cap attempts

# RAM tmp to avoid SD I/O latency
TMP_PATH       = "/dev/shm/shot.jpg"

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
    """Scaled, centered preview on LCD (use BILINEAR for speed)."""
    im = pil_img.convert("RGB").copy()
    im.thumbnail((WIDTH, HEIGHT), Image.BILINEAR)
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    x = (WIDTH - im.width) // 2
    y = (HEIGHT - im.height) // 2
    canvas.paste(im, (x, y))
    with lcd_lock:
        disp.display(canvas)
    sleep(seconds)

lcd_show_text("Ready", "Press button / Web")

# =============== WebP (<=100 KB) encoder helpers ===============
_last_good_q = 78  # heuristic starting point; updated after each success

def _encode_webp(img, quality):
    """Encode PIL image to WebP bytes with given quality."""
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=int(quality), method=WEBP_METHOD)
    return buf.getvalue()

def _quality_search_under_cap(img, max_bytes, q_min=Q_MIN, q_max=Q_MAX, start_q=None, max_steps=5):
    """
    Binary/stepped search for a quality <= max_bytes.
    Uses a biased start near last known good to cut attempts. Caps steps.
    Returns (bytes, quality, fits_under_cap_bool).
    """
    if start_q is None:
        start_q = max(q_min, min(q_max, _last_good_q))

    # Quick try at start_q
    data = _encode_webp(img, start_q)
    if len(data) <= max_bytes:
        return data, start_q, True

    # Boundaries for search
    lo, hi = q_min, q_max
    # If start_q too large, move hi left
    if len(data) > max_bytes:
        hi = start_q - 1

    best_bytes, best_q = None, None
    steps = 0
    while lo <= hi and steps < max_steps:
        mid = (lo + hi) // 2
        data = _encode_webp(img, mid)
        if len(data) <= max_bytes:
            best_bytes, best_q = data, mid
            lo = mid + 1
        else:
            hi = mid - 1
        steps += 1

    if best_bytes is not None:
        return best_bytes, best_q, True

    # Nothing fit within steps → try the floor as a fallback result
    data = _encode_webp(img, q_min)
    return data, q_min, False

def _downscale_to_limit(img, max_bytes, min_side=MIN_SIDE_PX):
    """
    Try a quick quality search; if not under cap, progressively downscale + retry.
    Uses BILINEAR for speed.
    """
    global _last_good_q

    work = img
    for step in range(MAX_DOWNSCALE_STEPS):
        data, q, ok = _quality_search_under_cap(work, max_bytes, start_q=_last_good_q)
        if ok:
            _last_good_q = q  # remember for next capture
            return work, data, q

        # Still too big at low quality → shrink
        w, h = work.size
        if min(w, h) <= min_side:
            # Force an extra 0.85 step if still too large
            scale = 0.85
        else:
            # Heuristic: scale by sqrt(target/current)
            ratio = max_bytes / max(len(data), 1)
            scale = max(0.70, min(0.92, math.sqrt(ratio)))
        new_w = max(min_side, int(w * scale))
        new_h = max(min_side, int(h * scale))
        if new_w >= w and new_h >= h:
            new_w = max(min_side, int(w * 0.9))
            new_h = max(min_side, int(h * 0.9))
        work = work.resize((new_w, new_h), Image.BILINEAR)

    # Final attempt after steps exhausted
    data, q, ok = _quality_search_under_cap(work, max_bytes, start_q=_last_good_q)
    _last_good_q = q
    return work, data, q

# =============== Capture logic ===============
btn = Button(BUTTON_BCM, pull_up=True, bounce_time=0.15)

def _list_webps_sorted():
    p = Path(PHOTOS_DIR)
    files = list(p.glob("*.webp"))
    files.sort(key=lambda x: x.stat().st_mtime)
    return files

def capture_once():
    lcd_show_text("Capturing...", datetime.now().strftime("%H:%M:%S"))

    cmd = [
        "libcamera-jpeg", "-n",
        "--width", str(CAP_W), "--height", str(CAP_H),
        "-o", TMP_PATH
    ]
    if AUTOFOCUS:
        cmd.extend(["--autofocus-mode", "continuous"])

    try:
        run(cmd, check=True)

        # Convert to grayscale from RAM tmp
        base_img = Image.open(TMP_PATH).convert("L")

        # Enforce 100 KB cap with faster WebP path
        final_img, webp_bytes, used_q = _downscale_to_limit(base_img, MAX_BYTES, min_side=MIN_SIDE_PX)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ts_path = os.path.join(PHOTOS_DIR, f"{ts}.webp")

        # Write timestamped + latest (same bytes, two writes but tiny)
        with open(ts_path, "wb") as f:
            f.write(webp_bytes)
        with open(LATEST_WEBP, "wb") as f:
            f.write(webp_bytes)

        # 3s LCD preview of the actual stored image
        lcd_show_preview(final_img, seconds=3.0)

        # Back to Ready
        lcd_show_text("Ready", "Press button / Web")
        print(f"Captured {ts_path}  (q≈{used_q}, bytes={len(webp_bytes)})")

        _broadcast({"type": "captured", "ts": int(datetime.now().timestamp())})
        return True, ts_path
    except CalledProcessError as e:
        lcd_show_text("Capture ERR", "See logs")
        sleep(1.0)
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
  <title>Pi BnW Cam (WebP ≤100 KB, fast)</title>
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
    <button id="captureBtn">Capture (fast WebP ≤100 KB)</button>
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

  <div class="section">
    <h2>Arweave uploads <span class="muted" id="countAr"></span></h2>
    <div id="gridAr" class="grid"></div>
  </div>

<script>
const btn = document.getElementById("captureBtn");
const statusEl = document.getElementById("status");
const img = document.getElementById("preview");
const gridLocal = document.getElementById("gridLocal");
const countLocal = document.getElementById("countLocal");
const gridAr = document.getElementById("gridAr");
const countAr = document.getElementById("countAr");

let promptTimer = null;
let promptEl = null;
function showUploadPrompt() {
  // Create a temporary prompt under the hero image for 5s
  clearUploadPrompt();
  const hero = document.querySelector(".hero");
  promptEl = document.createElement("div");
  promptEl.style.marginTop = "8px";
  let secs = 5;
  promptEl.innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;">
      <span class="muted">Upload to Arweave?</span>
      <button id="uploadNowBtn">Upload</button>
      <span class="muted" id="countdown">(5s)</span>
    </div>
  `;
  hero.appendChild(promptEl);
  const btnUpload = promptEl.querySelector('#uploadNowBtn');
  const cd = promptEl.querySelector('#countdown');
  btnUpload.addEventListener('click', async () => {
    btnUpload.disabled = true;
    cd.textContent = '(uploading…)';
    await uploadToArweave();
    clearUploadPrompt();
  });
  promptTimer = setInterval(() => {
    secs -= 1;
    if (secs <= 0) {
      clearUploadPrompt();
    } else {
      cd.textContent = `(${secs}s)`;
    }
  }, 1000);
}
function clearUploadPrompt(){
  if (promptTimer){ clearInterval(promptTimer); promptTimer = null; }
  if (promptEl){ promptEl.remove(); promptEl = null; }
}

async function capture() {
  btn.disabled = true;
  btn.textContent = "Capturing…";
  statusEl.textContent = "Capturing and encoding (fast)…";
  try {
    const r = await fetch("/capture", { method: "POST" });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "Unknown error");
    img.src = "/latest.webp?ts=" + Date.now();
    statusEl.textContent = "Done.";
    await refreshGallery();
    // After successful capture, show Arweave upload prompt for 5 seconds
    showUploadPrompt();
  } catch (e) {
    console.error(e);
    statusEl.textContent = e.message || "Failed to capture. Check server logs.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Capture (fast WebP ≤100 KB)";
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
        showUploadPrompt();
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

function renderArweave(items){
  gridAr.innerHTML = "";
  countAr.textContent = `(${items.length})`;
  for (const it of items){
    const dt = new Date(it.tsMs || Date.now());
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <a href="${it.url}" target="_blank" rel="noopener">
        <img class="thumb" loading="lazy" src="${it.url}" alt="${it.id}" />
      </a>
      <div class="meta">${dt.toLocaleString()}</div>
    `;
    gridAr.appendChild(card);
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

async function refreshArweave(){
  try{
    const r = await fetch("/arweave.json");
    const data = await r.json();
    if (!data.ok) throw new Error("Arweave list failed");
    renderArweave(data.items || []);
  }catch(e){
    console.error(e);
  }
}

async function uploadToArweave(){
  statusEl.textContent = "Uploading to Arweave…";
  try{
    const r = await fetch("/upload_arweave", { method: "POST" });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || 'Upload failed');
    statusEl.textContent = "Uploaded to Arweave.";
    await refreshArweave();
  }catch(e){
    console.error(e);
    statusEl.textContent = e.message || 'Upload failed';
  }
}

(async function init(){
  img.src = "/latest.webp?ts=" + Date.now();
  await refreshGallery();
  await refreshArweave();
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
    if not os.path.exists(LATEST_WEBP):
        abort(404)
    return send_file(LATEST_WEBP, mimetype="image/webp", as_attachment=False)

@app.route("/img/<path:name>")
def serve_image(name):
    safe = os.path.basename(name)
    target = os.path.join(PHOTOS_DIR, safe)
    if not os.path.exists(target):
        abort(404)
    return send_file(target, mimetype="image/webp", as_attachment=False)

@app.route("/gallery.json")
def gallery():
    items = []
    for f in _list_webps_sorted():
        st = f.stat()
        items.append({
            "name": f.name,
            "url": f"/img/{f.name}",
            "size": st.st_size,
            "mtimeMs": int(st.st_mtime * 1000),
        })
    return jsonify({"ok": True, "local": items})

@app.route("/arweave.json")
def arweave_list():
    try:
        if not os.path.exists(ARWEAVE_JSON):
            return jsonify({"ok": True, "items": []})
        with open(ARWEAVE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                data = []
        return jsonify({"ok": True, "items": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def _append_arweave_record(record):
    try:
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        existing = []
        if os.path.exists(ARWEAVE_JSON):
            with open(ARWEAVE_JSON, "r", encoding="utf-8") as f:
                try:
                    existing = json.load(f)
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
        existing.append(record)
        with open(ARWEAVE_JSON, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Failed to persist arweave.json:", e)

@app.route("/upload_arweave", methods=["POST"])
def upload_arweave():
    try:
        # choose latest file (use latest.webp if exists)
        src = LATEST_WEBP if os.path.exists(LATEST_WEBP) else None
        if not src or not os.path.exists(src):
            # fallback to newest .webp in folder
            files = _list_webps_sorted()
            if files:
                src = str(files[-1])
        if not src or not os.path.exists(src):
            return jsonify({"ok": False, "error": "No image available to upload"}), 400

        # call Node uploader with --json
        here = os.path.dirname(os.path.abspath(__file__))
        upload_js = os.path.join(here, "upload.js")
        if not os.path.exists(upload_js):
            return jsonify({"ok": False, "error": "upload.js not found"}), 500

        try:
            proc = run(["node", upload_js, "--json", src], check=True, stdout=PIPE, stderr=PIPE)
            out = proc.stdout.decode("utf-8", errors="ignore").strip()
            data = json.loads(out)
        except CalledProcessError as e:
            err = e.stderr.decode("utf-8", errors="ignore")
            return jsonify({"ok": False, "error": err or str(e)}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

        if not data.get("ok"):
            return jsonify({"ok": False, "error": data.get("error", "Upload failed")}), 500

        record = {
            "id": data.get("id"),
            "url": data.get("url"),
            "size": data.get("size"),
            "file": data.get("file"),
            "tsMs": int(datetime.now().timestamp() * 1000),
        }
        _append_arweave_record(record)
        return jsonify({"ok": True, "record": record})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/capture", methods=["POST"])
def capture():
    ok, info = capture_once()
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": info}), 500

def main():
    Thread(target=button_worker, daemon=True).start()
    print(f"Serving on http://0.0.0.0:{PORT}")
    try:
        from waitress import serve as waitress_serve
        waitress_serve(app, host="0.0.0.0", port=PORT)
    except Exception:
        app.run(host="0.0.0.0", port=PORT, debug=False)

if __name__ == "__main__":
    main()
