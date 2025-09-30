// upload_turbo.mjs
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

// Import the Node build of Turbo SDK:
import { ArweaveSigner, TurboFactory, setLogLevel } from "@ardrive/turbo-sdk/node";

// --- config ---
const BASE = "/home/cam/final-cam";
const WALLET_PATH = path.join(BASE, "wallet.json");
const FILE_PATH   = path.join(BASE, "test.webp");
const MAX_BYTES   = 100 * 1024;

// optional: see progress logs in your terminal
setLogLevel("info");  // 'error' | 'warn' | 'info' | 'debug'

function assertFile(p) {
  if (!fs.existsSync(p)) {
    console.error(`[!] Not found: ${p}`);
    process.exit(1);
  }
}

async function main() {
  assertFile(WALLET_PATH);
  assertFile(FILE_PATH);

  const size = fs.statSync(FILE_PATH).size;
  if (size > MAX_BYTES) {
    console.error(`[!] ${path.basename(FILE_PATH)} is ${size} bytes (> 100 KB). Please shrink it first.`);
    process.exit(1);
  }

  console.log("[*] Loading wallet…");
  const jwk = JSON.parse(fs.readFileSync(WALLET_PATH, "utf-8"));
  const signer = new ArweaveSigner(jwk);

  console.log("[*] Creating Turbo client…");
  const turbo = TurboFactory.authenticated({ signer }); // Node auth client (JWK signer) :contentReference[oaicite:1]{index=1}

  console.log("[*] Uploading file via Turbo…");
  const fileSize = size;
  const filePath = FILE_PATH;

  const result = await turbo.uploadFile({
    fileStreamFactory: () => fs.createReadStream(filePath),
    fileSizeFactory: () => fileSize,
    dataItemOpts: {
      // Tags tell gateways how to serve the bytes
      tags: [{ name: "Content-Type", value: "image/webp" }],
      // If someone else shared Turbo Credits with you, you can pay with their wallet(s):
      // paidBy: ["<AR or native address>", "..."],
    },
    events: {
      onProgress: ({ totalBytes, processedBytes, step }) => {
        process.stdout.write(
          `\r[progress] ${step} ${processedBytes}/${totalBytes} bytes      `
        );
      },
      onError: ({ error, step }) => {
        console.error(`\n[error] step=${step}`, error);
      },
      onUploadSuccess: () => console.log("\n[+] Upload success!"),
    },
  });

  // result has the DataItem ID (Arweave tx id after bundling)
  console.log("\n---");
  console.log("Data Item ID:", result.id);
  console.log("Gateway URL:  https://arweave.net/" + result.id);
}

main().catch((e) => {
  console.error("\n[!] Failed:", e);
  process.exit(1);
});
