#!/usr/bin/env node
// upload_one.js — simple Turbo upload using the same pattern as your server code

const fs = require("fs");
const path = require("path");

const ROOT = __dirname;
const WALLET = path.join(ROOT, "wallet.json");
const DEFAULT_FILE = path.join(ROOT, "test.webp");
const MAX_BYTES = 100 * 1024;

// dynamic import so we can stay in CommonJS
async function ensureTurbo(jwk) {
  const mod = await import("@ardrive/turbo-sdk");
  const { TurboFactory } = mod;
  // same style you used before: auth with privateKey (JWK)
  return TurboFactory.authenticated({ privateKey: jwk });
}

function assertFile(p) {
  if (!fs.existsSync(p)) {
    console.error(`[!] Not found: ${p}`);
    process.exit(1);
  }
}

(async () => {
  // pick file from argv or default
  const filePath = process.argv[2] ? path.resolve(process.argv[2]) : DEFAULT_FILE;

  assertFile(WALLET);
  assertFile(filePath);

  const size = fs.statSync(filePath).size;
  if (size > MAX_BYTES) {
    console.error(`[!] ${path.basename(filePath)} is ${size} bytes (> 100 KB). Please shrink it first.`);
    process.exit(1);
  }

  console.log("[*] Loading wallet…");
  const jwk = JSON.parse(fs.readFileSync(WALLET, "utf8"));

  console.log("[*] Authenticating Turbo client…");
  const turbo = await ensureTurbo(jwk);

  console.log("[*] Uploading:", filePath);
  const result = await turbo.uploadFile({
    fileStreamFactory: () => fs.createReadStream(filePath),
    fileSizeFactory: () => size,
    dataItemOpts: {
      tags: [{ name: "Content-Type", value: "image/webp" }],
      // If you have shared credits, you can add:
      // paidBy: ["<address1>", "<address2>"],
    },
    events: {
      onProgress: ({ totalBytes, processedBytes, step }) =>
        process.stdout.write(`\r[progress] ${step} ${processedBytes}/${totalBytes} bytes       `),
      onError: ({ error, step }) =>
        console.error(`\n[error] step=${step}`, error?.message || error),
      onUploadSuccess: () => console.log("\n[+] Upload success!"),
    },
  });

  console.log("\n---");
  console.log("Data Item ID:", result.id);
  console.log("Gateway URL:  https://arweave.net/" + result.id);
})().catch((e) => {
  console.error("\n[!] Failed:", e?.message || e);
  process.exit(1);
});
