// pixel.js (or server.js)
// Run: sudo -E node pixel.js
const express = require("express");
const { exec } = require("child_process");
const path = require("path");
const fs = require("fs");
const mime = require("mime-types");

// ====== GPIO (buttons) via pigpio ======
const { Gpio } = require("pigpio");

// ====== OLED (SSD1306 @ 0x3C) ======
const i2c = require("i2c-bus");
const Oled = require("oled-i2c-bus");
const font = require("oled-font-5x7");

process.on("unhandledRejection", (reason) => {
  console.error("UNHANDLED REJECTION:", reason);
});
process.on("uncaughtException", (err) => {
  console.error("UNCAUGHT EXCEPTION:", err);
  // Do not exit so the server keeps running; flip to process.exit(1) if you prefer a hard crash.
});

const app = express();
const PORT = process.env.PORT || 3000;

const ROOT_DIR = __dirname;
const PUBLIC_DIR = path.join(ROOT_DIR, "public");
const OUTPUT_PATH = path.join(PUBLIC_DIR, "latest.webp");
const IMAGES_DIR = path.join(ROOT_DIR, "images");

// Ensure folders
fs.mkdirSync(PUBLIC_DIR, { recursive: true });
fs.mkdirSync(IMAGES_DIR, { recursive: true });

// ---------- State (daily shots) ----------
const STATE_PATH = path.join(ROOT_DIR, "state.json");
const DAILY_LIMIT = 10;

function todayLocalISODate() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}
function nowStamp() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}_${hh}-${mi}-${ss}`;
}
function defaultState() {
  return { date: todayLocalISODate(), shotsRemaining: DAILY_LIMIT };
}
function readState() {
  try {
    const raw = fs.readFileSync(STATE_PATH, "utf8");
    const obj = JSON.parse(raw);
    if (!obj || typeof obj.date !== "string" || typeof obj.shotsRemaining !== "number") {
      throw new Error("Invalid state");
    }
    return obj;
  } catch {
    const s = defaultState();
    writeState(s);
    return s;
  }
}
function writeState(state) {
  const tmp = STATE_PATH + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2));
  fs.renameSync(tmp, STATE_PATH);
}
function ensureToday(state) {
  const t = todayLocalISODate();
  if (state.date !== t) {
    state.date = t;
    state.shotsRemaining = DAILY_LIMIT;
    writeState(state);
  }
}
function canCapture(state) {
  ensureToday(state);
  return state.shotsRemaining > 0;
}
function decAndPersist(state) {
  state.shotsRemaining = Math.max(0, state.shotsRemaining - 1);
  writeState(state);
}

// ---------- Arweave manifest (manual upload only) ----------
const ARW_MANIFEST = path.join(ROOT_DIR, "arweave.json");
function readManifest() {
  try {
    const raw = fs.readFileSync(ARW_MANIFEST, "utf8");
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) throw new Error("bad manifest");
    return arr;
  } catch {
    fs.writeFileSync(ARW_MANIFEST, "[]");
    return [];
  }
}
function writeManifest(arr) {
  const tmp = ARW_MANIFEST + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(arr, null, 2));
  fs.renameSync(tmp, ARW_MANIFEST);
}
function upsertManifestEntry(filename, patch) {
  const m = readManifest();
  const i = m.findIndex(x => x.filename === filename);
  const base = i >= 0 ? m[i] : { filename, status: "none" };
  const next = { ...base, ...patch };
  if (i >= 0) m[i] = next; else m.push(next);
  writeManifest(m);
  return next;
}
function getManifestEntry(filename) {
  return readManifest().find(x => x.filename === filename);
}

// Upload queue (manual only)
const uploadQueue = [];
let isUploading = false;
let turbo = null;
async function ensureTurbo() {
  if (turbo) return turbo;
  const walletPath = path.join(ROOT_DIR, "wallet.json");
  const jwk = JSON.parse(fs.readFileSync(walletPath, "utf-8"));
  const mod = await import("@ardrive/turbo-sdk");
  const { TurboFactory } = mod;
  turbo = await TurboFactory.authenticated({ privateKey: jwk });
  return turbo;
}
function enqueueUpload(filename) {
  if (!filename) return;
  const entry = getManifestEntry(filename);
  if (entry && entry.status === "success") return;
  upsertManifestEntry(filename, { status: "pending", queuedAt: new Date().toISOString() });
  uploadQueue.push(filename);
  process.nextTick(processUploadQueue);
}
async function processUploadQueue() {
  if (isUploading) return;
  isUploading = true;
  try {
    await ensureTurbo();
  } catch (e) {
    console.error("Arweave auth failed:", e);
    isUploading = false;
    return;
  }

  while (uploadQueue.length) {
    const filename = uploadQueue.shift();
    const filePath = path.join(IMAGES_DIR, filename);
    try {
      if (!fs.existsSync(filePath)) {
        const ent = getManifestEntry(filename);
        if (!ent || ent.status !== "success") {
          upsertManifestEntry(filename, { status: "failed", error: "File missing", failedAt: new Date().toISOString() });
        }
        continue;
      }
      const st = fs.statSync(filePath);
      const type = mime.lookup(filePath) || "application/octet-stream";

      const result = await turbo.uploadFile({
        fileStreamFactory: () => fs.createReadStream(filePath),
        fileSizeFactory: () => st.size,
        dataItemOpts: { tags: [{ name: "Content-Type", value: String(type) }] },
      });

      const url = `https://arweave.net/${result.id}`;
      try { fs.unlinkSync(filePath); } catch {}

      upsertManifestEntry(filename, {
        status: "success",
        uploadedAt: new Date().toISOString(),
        txId: result.id,
        url,
        size: st.size,
      });

      broadcast({ type: "uploaded", filename, url, txId: result.id });
      console.log("Arweave:", filename, url);
    } catch (e) {
      console.error("Upload failed:", filename, e?.message || e);
      upsertManifestEntry(filename, {
        status: "failed",
        error: String(e?.message || e),
        failedAt: new Date().toISOString(),
      });
    }
  }
  isUploading = false;
}

// ---------- Static + APIs ----------
app.use(
  express.static(PUBLIC_DIR, {
    setHeaders: (res, p) => {
      if (p.endsWith("latest.webp")) res.setHeader("Cache-Control", "no-store");
    },
  })
);
app.use("/images", express.static(IMAGES_DIR, { maxAge: "1d" }));

// Gallery split: local (disk) + archived (arweave)
app.get("/gallery.json", async (_req, res) => {
  try {
    const names = (await fs.promises.readdir(IMAGES_DIR)).filter(n => n.toLowerCase().endsWith(".webp"));
    const locals = await Promise.all(
      names.map(async (name) => {
        const full = path.join(IMAGES_DIR, name);
        const st = await fs.promises.stat(full);
        return {
          name,
          url: `/images/${encodeURIComponent(name)}`,
          mtimeMs: st.mtimeMs,
          size: st.size,
        };
      })
    );
    locals.sort((a, b) => b.mtimeMs - a.mtimeMs);

    const archived = readManifest()
      .filter((m) => m.status === "success" && m.url)
      .map((m) => ({
        name: m.filename,
        url: m.url,
        txId: m.txId,
        uploadedAt: m.uploadedAt || null,
        size: m.size || null,
      }))
      .sort((a, b) => {
        const at = a.uploadedAt ? Date.parse(a.uploadedAt) : 0;
        const bt = b.uploadedAt ? Date.parse(b.uploadedAt) : 0;
        return bt - at;
      });

    res.json({ ok: true, local: locals, archived });
  } catch (e) {
    console.error("gallery.json error:", e);
    res.status(500).json({ ok: false, error: "Failed to read gallery" });
  }
});

// ---------- SSE ----------
const clients = new Set();
app.get("/events", (req, res) => {
  res.set({
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
  });
  res.flushHeaders();
  res.write("retry: 2000\n\n");
  clients.add(res);
  req.on("close", () => clients.delete(res));
});
function broadcast(obj) {
  const payload = `data: ${JSON.stringify(obj)}\n\n`;
  for (const res of clients) {
    try { res.write(payload); } catch {}
  }
}
function notifyCaptured(filename = null) {
  broadcast({ type: "captured", ts: Date.now(), filename });
}

// ---------- Capture pipeline (with FIXES) ----------
let isBusy = false;

// FIX #1: reject with Error, not plain object
function safeExec(cmd, opts = {}) {
  return new Promise((resolve, reject) => {
    exec(cmd, { timeout: 15000, ...opts }, (err, stdout, stderr) => {
      if (err) {
        const e = new Error(String(stderr || err.message || "exec failed"));
        e.stderr = String(stderr || "");
        e.cause = err;
        return reject(e);
      }
      resolve({ stdout: String(stdout), stderr: String(stderr) });
    });
  });
}

async function captureImage() {
  const cmd = `rpicam-still -n -t 1 -o - | convert - -resize '1024x1024>' -colorspace Gray -auto-level -contrast-stretch 0.5%x0.5% -define webp:lossless=false -quality 80 -define webp:method=6 -define webp:target-size=100000 "${OUTPUT_PATH}"`;
  await safeExec(cmd);
  const name = `${nowStamp()}.webp`;
  fs.copyFileSync(OUTPUT_PATH, path.join(IMAGES_DIR, name));
  return name;
}

// ---------- OLED + Twinkle idle ----------
let oled = null, oledBus = null;
let twinkleTimer = null, twinkleEnabled = false, stars = [];
const FPS = 5, STAR_COUNT = 10, MOVE_EVERY_MS = 3000;
let lastMoveAt = 0;
const BOXES = [{ x0: 104, y0: 0, x1: 127, y1: 15 }, { x0: 0, y0: 48, x1: 27, y1: 63 }];

function randInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
function randomPointInBox(b) { return { x: randInt(b.x0, b.x1), y: randInt(b.y0, b.y1) }; }

function initOled() {
  try {
    oledBus = i2c.openSync(1);
    oled = new Oled(oledBus, { width: 128, height: 64, address: 0x3c });
    oled.clearDisplay();
    oled.turnOnDisplay();
    return true;
  } catch (e) {
    console.error("OLED init failed:", e.message || e);
    oled = null;
    return false;
  }
}
function showStatus(text) {
  if (!oled) return;
  stopTwinkle();
  oled.clearDisplay();
  oled.setCursor(0, 0);
  oled.writeString(font, 1, text, 1, true);
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function seedStars() {
  stars = [];
  for (let i = 0; i < STAR_COUNT; i++) {
    const box = BOXES[i % BOXES.length];
    const p = randomPointInBox(box);
    stars.push({ x: p.x, y: p.y, on: Math.random() < 0.5 });
  }
}
function showRemainingBig(n) {
  if (!oled) return;
  stopTwinkle();
  oled.clearDisplay();
  oled.setCursor(0, 0);
  oled.writeString(font, 1, "Shots left", 1, true);

  const s = String(n), size = 3;
  const charW = 5 * size + 1, charH = 7 * size;
  const totalW = charW * s.length;
  const x = Math.max(0, Math.floor((128 - totalW) / 2));
  const y = Math.max(0, Math.floor((64 - charH) / 2));
  oled.setCursor(x, y);
  oled.writeString(font, size, s, 1, true);

  startTwinkle();
}
async function showActiveCountdown(seconds = 3) {
  if (!oled) { await sleep(seconds * 1000); return; }
  stopTwinkle();
  for (let s = seconds; s >= 1; s--) {
    oled.clearDisplay();
    oled.setCursor(0, 0);
    oled.writeString(font, 1, "Hold steady…", 1, true);
    const big = String(s), size = 3;
    const charW = 5 * size + 1, charH = 7 * size;
    const x = Math.max(0, Math.floor((128 - charW) / 2));
    const y = Math.max(0, Math.floor((64 - charH) / 2));
    oled.setCursor(x, y);
    oled.writeString(font, size, big, 1, true);
    await sleep(1000);
  }
}
function showResult(ok, msg = "") {
  if (!oled) return;
  stopTwinkle();
  oled.clearDisplay();
  oled.setCursor(0, 0);
  if (ok) {
    oled.writeString(font, 1, "Saved ✓", 1, true);
  } else {
    oled.writeString(font, 1, "Error", 1, true);
    if (msg) {
      oled.setCursor(0, 16);
      oled.writeString(font, 1, msg.slice(0, 21), 1, true);
    }
  }
}
function startTwinkle() {
  if (!oled || twinkleEnabled) return;
  twinkleEnabled = true;
  seedStars();
  lastMoveAt = Date.now();

  if (twinkleTimer) clearInterval(twinkleTimer);
  twinkleTimer = setInterval(() => {
    if (!twinkleEnabled || !oled) return;
    const toDraw = [];
    for (const star of stars) {
      toDraw.push([star.x, star.y, 0]);
      if (Math.random() < 0.5) star.on = !star.on;
      if (star.on) toDraw.push([star.x, star.y, 1]);
    }
    const now = Date.now();
    if (now - lastMoveAt > MOVE_EVERY_MS) {
      lastMoveAt = now;
      const moves = Math.max(1, Math.round(STAR_COUNT * 0.2));
      for (let i = 0; i < moves; i++) {
        const idx = randInt(0, stars.length - 1);
        const box = BOXES[idx % BOXES.length];
        const p = randomPointInBox(box);
        stars[idx].x = p.x; stars[idx].y = p.y; stars[idx].on = true;
        toDraw.push([p.x, p.y, 1]);
      }
    }
    try { oled.drawPixel(toDraw); } catch {}
    try { oled.update(); } catch {}
  }, 1000 / FPS);
}
function stopTwinkle() {
  twinkleEnabled = false;
  if (twinkleTimer) { clearInterval(twinkleTimer); twinkleTimer = null; }
  if (oled && stars && stars.length) {
    const erase = stars.map(s => [s.x, s.y, 0]);
    try { oled.drawPixel(erase); oled.update(); } catch {}
  }
}

// Full capture UI flow (NO auto-upload) with FIX #2 (self-catching capture promise)
async function runCaptureWithUI(state) {
  // Start capture immediately, but PREVENT unhandled rejection:
  let capError = null;
  const capPromise = captureImage().catch((e) => {
    capError = e;   // store error so it won't crash as unhandled
    return null;    // make the promise resolve -> prevents unhandled rejection
  });

  // Show countdown while capture runs
  await showActiveCountdown(3);

  // Processing splash
  showStatus("Processing...");
  await sleep(10000);

  // Surface any capture error now (in the right UI place)
  if (capError) {
    showResult(false, "Capture error");
    throw capError;
  }

  // capPromise has either resolved to filename or null; get the filename
  const filename = await capPromise; // safe now

  // Decrement quota & UI
  decAndPersist(state);
  showResult(true);
  notifyCaptured(filename);

  setTimeout(() => {
    const fresh = readState(); ensureToday(fresh);
    showRemainingBig(fresh.shotsRemaining);
  }, 800);

  return filename;
}

// ------------- Buttons via ALERT (no ISR interrupts) -------------
let btnA, btnB;
function initButtons() {
  try {
    btnA = new Gpio(17, { mode: Gpio.INPUT, pullUpDown: Gpio.PUD_UP });
    btnB = new Gpio(27, { mode: Gpio.INPUT, pullUpDown: Gpio.PUD_UP });
    btnA.glitchFilter(10000); btnB.glitchFilter(10000);
    btnA.enableAlert(); btnB.enableAlert();

    btnA.on("alert", async (level) => {
      if (level !== 0) return;
      if (isBusy) { showStatus("Busy…"); return; }
      const state = readState(); ensureToday(state);
      if (!canCapture(state)) { showStatus("Limit reached"); setTimeout(() => showRemainingBig(state.shotsRemaining), 1500); return; }
      isBusy = true;
      try { await runCaptureWithUI(state); }
      catch (e) {
        console.error("Button capture failed:", e?.stderr || e);
        showResult(false, "Check camera");
        setTimeout(() => showRemainingBig(readState().shotsRemaining), 1500);
      } finally { isBusy = false; }
    });

    // Button B reserved for future actions
    btnB.on("alert", (level) => { if (level !== 0) return; console.log("Button B pressed"); });

    console.log("Buttons ready on GPIO17 (A) and GPIO27 (B) [ALERT mode].");
    return true;
  } catch (e) {
    console.error("Button init failed:", e.message || e);
    return false;
  }
}

// ------------- HTTP routes -------------
app.post("/capture", async (_req, res) => {
  if (isBusy) return res.status(409).json({ ok: false, error: "Busy" });
  const state = readState(); ensureToday(state);
  if (!canCapture(state)) {
    showStatus("Limit reached"); setTimeout(() => showRemainingBig(state.shotsRemaining), 1500);
    return res.status(403).json({ ok: false, error: "Daily limit reached" });
  }
  isBusy = true;
  try {
    const filename = await runCaptureWithUI(state);
    return res.json({ ok: true, url: `/latest.webp?ts=${Date.now()}`, saved: `/images/${encodeURIComponent(filename)}` });
  } catch (e) {
    console.error("Capture error:", e?.stderr || e);
    showResult(false, "Capture failed");
    setTimeout(() => showRemainingBig(readState().shotsRemaining), 1500);
    return res.status(500).json({ ok: false, error: "Capture failed" });
  } finally {
    isBusy = false;
  }
});

// Manual upload ALL local images (each success deletes local file)
app.post("/upload-all", async (_req, res) => {
  try {
    const names = (await fs.promises.readdir(IMAGES_DIR)).filter(n => n.toLowerCase().endsWith(".webp"));
    names.forEach(n => enqueueUpload(n));
    res.json({ ok: true, queued: names.length });
  } catch (e) {
    console.error("/upload-all error:", e);
    res.status(500).json({ ok: false, error: "Failed to queue uploads" });
  }
});

// Optional: retry a specific filename
app.post("/upload/:filename", async (req, res) => {
  try {
    const filename = decodeURIComponent(req.params.filename);
    const full = path.join(IMAGES_DIR, filename);
    if (!fs.existsSync(full)) return res.status(404).json({ ok: false, error: "File not found" });
    enqueueUpload(filename);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: "Failed to queue file" });
  }
});

// ------------- Startup -------------
const oledOk = initOled();
const bootState = readState(); ensureToday(bootState);
showRemainingBig(bootState.shotsRemaining);
const buttonsOk = initButtons();

app.listen(PORT, () => {
  console.log(`Pi BnW cam listening on http://localhost:${PORT}`);
  if (!oledOk) console.log("OLED not available; continuing without display.");
  if (!buttonsOk) console.log("Buttons not available; continuing without GPIO.");
});

// ------------- Cleanup -------------
process.on("SIGINT", () => {
  try { stopTwinkle(); if (oled) { oled.clearDisplay(); oled.turnOffDisplay(); if (oledBus) oledBus.closeSync(); } } catch {}
  try { if (btnA?.disableAlert) btnA.disableAlert(); if (btnB?.disableAlert) btnB.disableAlert(); } catch {}
  process.exit(0);
});
