// upload.cjs (CommonJS)
const fs = require("fs");
const path = require("path");
// Use the Node CJS build explicitly:
const { ArweaveSigner, TurboFactory } = require("@ardrive/turbo-sdk/node/cjs");

const BASE = "/home/hope/final-cam";        // adjust if needed
const WALLET_PATH = path.join(BASE, "wallet.json");
const FILE_PATH   = path.join(BASE, "test.webp");
const MAX_BYTES   = 100 * 1024;

function assertFile(p) {
  if (!fs.existsSync(p)) {
    console.error(`[!] Not found: ${p}`);
    process.exit(1);
  }
}

(async () => {
  assertFile(WALLET_PATH);
  assertFile(FILE_PATH);

  const size = fs.statSync(FILE_PATH).size;
  if (size > MAX_BYTES) {
    console.error(`[!] ${path.basename(FILE_PATH)} is ${size} bytes (> 100 KB). Shrink it first.`);
    process.exit(1);
  }

  console.log("[*] Loading wallet…");
  const jwk = JSON.parse(fs.readFileSync(WALLET_PATH, "utf-8"));
  const signer = new ArweaveSigner(jwk);

  console.log("[*] Creating Turbo client…");
  const turbo = TurboFactory.authenticated({ signer });

  console.log("[*] Uploading via Turbo…");
  const result = await turbo.uploadFile({
    fileStreamFactory: () => fs.createReadStream(FILE_PATH),
    fileSizeFactory: () => size,
    dataItemOpts: { tags: [{ name: "Content-Type", value: "image/webp" }] },
    events: {
      onProgress: ({ totalBytes, processedBytes, step }) =>
        process.stdout.write(`\r[progress] ${step} ${processedBytes}/${totalBytes} bytes    `),
      onError: ({ error, step }) => console.error(`\n[error] step=${step}`, error),
      onUploadSuccess: () => console.log("\n[+] Upload success!"),
    },
  });

  console.log("\n---");
  console.log("Data Item ID:", result.id);
  console.log("Gateway URL:  https://arweave.net/" + result.id);
})().catch((e) => {
  console.error("\n[!] Failed:", e);
  process.exit(1);
});
