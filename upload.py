#!/usr/bin/env python3
import sys, time
from pathlib import Path

# deps: pip install arweave-python-client requests
from arweave import Wallet, Transaction
import requests

# --- config ---
BASE = Path("/home/cam/final-cam")
WALLET_PATH = BASE / "wallet.json"
FILE_PATH   = BASE / "test.webp"
CONTENT_TYPE = "image/webp"
MAX_BYTES = 100 * 1024  # sanity check (100 KB)

def die(msg, code=1):
    print(f"[!] {msg}", file=sys.stderr); sys.exit(code)

def main():
    if not WALLET_PATH.exists():
        die(f"Wallet not found: {WALLET_PATH}")
    if not FILE_PATH.exists():
        die(f"File not found: {FILE_PATH}")

    size = FILE_PATH.stat().st_size
    if size > MAX_BYTES:
        die(f"{FILE_PATH.name} is {size} bytes (> 100 KB). Please shrink it first.")

    print("[*] Loading wallet…")
    wallet = Wallet(str(WALLET_PATH))

    data = FILE_PATH.read_bytes()

    print("[*] Creating transaction…")
    tx = Transaction(wallet, data=data)
    tx.add_tag("Content-Type", CONTENT_TYPE)

    print("[*] Signing…")
    tx.sign()

    print("[*] Sending…")
    tx.send()

    print("\n[+] Sent!")
    print(f"    Tx ID: {tx.id}")
    print(f"    View:  https://arweave.net/{tx.id}")
    print(f"    Info:  https://arweave.net/tx/{tx.id}")

    # Optional: quick status polling
    for i in range(6):
        time.sleep(3)
        try:
            r = requests.get(f"https://arweave.net/tx/{tx.id}/status", timeout=10)
            if r.ok:
                j = r.json()
                status = j.get("status")
                confirmed = j.get("confirmed")
                print(f"[poll {i+1}] status={status} confirmed={bool(confirmed)}")
                if status == 200 and confirmed:
                    print("[✓] Confirmed on-chain."); break
            else:
                print(f"[poll {i+1}] HTTP {r.status_code}")
        except Exception as e:
            print(f"[poll {i+1}] {e}")

if __name__ == "__main__":
    main()
