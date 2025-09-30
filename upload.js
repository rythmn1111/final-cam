#!/usr/bin/env node
// upload.js — Turbo upload helper for Arweave (CJS) with optional --json output

const fs = require("fs");
const path = require("path");

const ROOT = __dirname;
const WALLET = path.join(ROOT, "wallet.json");
const DEFAULT_FILE = path.join(ROOT, "test.webp");
const MAX_BYTES = 100 * 1024;

// Load environment variables from .env.local or .env if present
try {
  const dotenv = require("dotenv");
  const envLocal = path.join(ROOT, ".env.local");
  const envDefault = path.join(ROOT, ".env");
  if (fs.existsSync(envLocal)) dotenv.config({ path: envLocal });
  else if (fs.existsSync(envDefault)) dotenv.config({ path: envDefault });
} catch {}

// dynamic import so we can stay in CommonJS
async function ensureTurbo(jwk) {
  const mod = await import("@ardrive/turbo-sdk");
  const { TurboFactory } = mod;
  return TurboFactory.authenticated({ privateKey: jwk });
}

function assertFile(p) {
  if (!fs.existsSync(p)) {
    console.error(`[!] Not found: ${p}`);
    process.exit(1);
  }
}

(async () => {
  const argv = process.argv.slice(2);
  const IS_JSON = argv.includes("--json");
  const filtered = argv.filter((a) => a !== "--json");

  const filePath = filtered[0] ? path.resolve(filtered[0]) : DEFAULT_FILE;

  assertFile(WALLET);
  assertFile(filePath);

  const size = fs.statSync(filePath).size;
  if (size > MAX_BYTES) {
    const msg = `${path.basename(filePath)} is ${size} bytes (> 100 KB). Please shrink it first.`;
    if (IS_JSON) {
      console.log(JSON.stringify({ ok: false, error: msg }));
    } else {
      console.error(`[!] ${msg}`);
    }
    process.exit(1);
  }

  if (!IS_JSON) console.log("[*] Loading wallet…");
  const jwk = JSON.parse(fs.readFileSync(WALLET, "utf8"));

  if (!IS_JSON) console.log("[*] Authenticating Turbo client…");
  const turbo = await ensureTurbo(jwk);

  if (!IS_JSON) console.log("[*] Uploading:", filePath);
  const startedAt = Date.now();
  const result = await turbo.uploadFile({
    fileStreamFactory: () => fs.createReadStream(filePath),
    fileSizeFactory: () => size,
    dataItemOpts: {
      tags: [{ name: "Content-Type", value: "image/webp" }],
    },
    events: {
      onProgress: ({ totalBytes, processedBytes, step }) => {
        if (!IS_JSON) process.stdout.write(`\r[progress] ${step} ${processedBytes}/${totalBytes} bytes       `);
      },
      onError: ({ error, step }) => {
        if (!IS_JSON) console.error(`\n[error] step=${step}`, error?.message || error);
      },
      onUploadSuccess: () => {
        if (!IS_JSON) console.log("\n[+] Upload success!");
      },
    },
  });

  const payload = {
    ok: true,
    id: result.id,
    url: `https://arweave.net/${result.id}`,
    size,
    startedAt,
    finishedAt: Date.now(),
    file: path.basename(filePath),
    supabase: { ok: false, error: null },
  };
  // Optional: insert into Supabase if env vars are present
  try {
    const supaUrl = process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL;
    const supaKey = process.env.SUPABASE_SERVICE_KEY || process.env.SUPABASE_ANON_KEY || process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
    const table = process.env.SUPABASE_TABLE || "links";
    const linkColumn = process.env.SUPABASE_LINK_COLUMN || "links";
    if (supaUrl && supaKey) {
      const { createClient } = await import("@supabase/supabase-js");
      const supabase = createClient(supaUrl, supaKey, { auth: { persistSession: false } });
      const insertData = { [linkColumn]: payload.url };
      const { error } = await supabase.from(table).insert(insertData);
      if (error) {
        payload.supabase.ok = false;
        payload.supabase.error = error.message || String(error);
        // Always emit to stderr for diagnostics
        try { console.error("[supabase] insert error:", payload.supabase.error); } catch {}
      } else {
        payload.supabase.ok = true;
      }
    } else if (!IS_JSON) {
      console.log("[supabase] Skipped insert (missing SUPABASE_URL or key)");
    }
  } catch (e) {
    const msg = e?.message || String(e);
    payload.supabase.ok = false;
    payload.supabase.error = msg;
    try { console.error("[supabase] insert failed:", msg); } catch {}
  }
  if (IS_JSON) {
    console.log(JSON.stringify(payload));
  } else {
    console.log("\n---");
    console.log("Data Item ID:", result.id);
    console.log("Gateway URL:  https://arweave.net/" + result.id);
  }
})().catch((e) => {
  const msg = e?.message || e;
  try {
    if (process.argv.includes("--json")) {
      console.log(JSON.stringify({ ok: false, error: String(msg) }));
    } else {
      console.error("\n[!] Failed:", msg);
    }
  } catch {}
  process.exit(1);
});
