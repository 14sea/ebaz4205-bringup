#!/usr/bin/env python3
"""Capture base64-encoded data from the board over UART.

Board-side: prints `BEGIN_B64\\n<base64>\\nEND_B64\\n` on its console.
Host-side: this script reads from /dev/ebaz-uart, finds the markers,
decodes the body and writes raw bytes to <out>.

Usage:
  uart-capture-b64.py --out backup/boot.bin --expect 3145728 \\
      --send 'base64 /tmp/boot.bin'
"""
import argparse, base64, hashlib, os, sys, time
import serial

ap = argparse.ArgumentParser()
ap.add_argument('--port', default='/dev/ebaz-uart')
ap.add_argument('--baud', type=int, default=115200)
ap.add_argument('--out', required=True)
ap.add_argument('--expect', type=int, default=0,
                help='expected decoded byte count (0 = no check)')
ap.add_argument('--md5', default='', help='expected md5 of decoded payload')
ap.add_argument('--send', required=True, help='shell command to run on board')
ap.add_argument('--timeout', type=float, default=600.0)
ap.add_argument('--progress', type=int, default=262144,
                help='print a tick every N bytes received')
args = ap.parse_args()

BEGIN = b'BEGIN_B64'
END = b'END_B64'
# After base64 actually finishes, the shell prints END_B64 then its prompt.
# The command echo has END_B64 embedded mid-line, so wait for END_B64\r\n then a `# ` prompt.
END_FINAL = b'END_B64\r\n# '

s = serial.Serial(args.port, args.baud, timeout=0.2)
# drain anything pending so we line up cleanly
s.reset_input_buffer()
cmd = f"echo {BEGIN.decode()}; {args.send}; echo {END.decode()}\r"
s.write(cmd.encode())
s.flush()

buf = bytearray()
deadline = time.time() + args.timeout
last_tick = 0
while time.time() < deadline:
    chunk = s.read(8192)
    if chunk:
        buf.extend(chunk)
        if len(buf) - last_tick >= args.progress:
            last_tick = len(buf)
            sys.stdout.write(f"  rx={len(buf)}\n"); sys.stdout.flush()
        if END_FINAL in buf:
            break
else:
    print(f"timeout: rx={len(buf)} bytes, never saw END marker", file=sys.stderr)
    sys.exit(2)

s.close()

# The real END is the one followed by '\r\n# ' (shell prompt).
# The real BEGIN is the LAST occurrence of BEGIN before that — which is the
# echo from the on-board `echo BEGIN_B64`, not the command echo (since that
# one has BEGIN embedded mid-line, not at line start). Use the BEGIN that has
# a newline immediately before it; if none, fall back to rindex.
final_end = buf.rindex(END_FINAL)
search_pat = b'\nBEGIN_B64\r\n'
b_idx = buf.rfind(search_pat, 0, final_end)
if b_idx < 0:
    # fallback: very first BEGIN after some newline
    b_idx = buf.find(search_pat)
if b_idx < 0:
    print("could not locate BEGIN_B64 on its own line", file=sys.stderr)
    sys.exit(6)
body_start = b_idx + len(search_pat)
body = bytes(buf[body_start:final_end])

# Always save the raw capture for post-mortem.
raw_path = args.out + '.raw'
os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
with open(raw_path, 'wb') as f:
    f.write(buf)

# Strict whitelist: only keep characters that are part of the base64 alphabet.
# Everything else (CR/LF/space/escape sequences/anything weird) is dropped.
ALPHABET = set(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=')
clean = bytes(c for c in body if c in ALPHABET)
# Drop trailing partial group if needed (truncate to multiple of 4).
clean = clean[: len(clean) - (len(clean) % 4)]
print(f"body chars (filtered) = {len(clean)}")
try:
    raw = base64.b64decode(clean, validate=False)
except Exception as e:
    print(f"decode error: {e} (raw saved to {raw_path})", file=sys.stderr)
    sys.exit(3)

os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
with open(args.out, 'wb') as f:
    f.write(raw)

m = hashlib.md5(raw).hexdigest()
print(f"wrote {len(raw)} bytes -> {args.out}")
print(f"md5: {m}")
if args.expect and len(raw) != args.expect:
    print(f"size mismatch: got {len(raw)}, expected {args.expect}", file=sys.stderr)
    sys.exit(4)
if args.md5 and m != args.md5:
    print(f"md5 mismatch: got {m}, expected {args.md5}", file=sys.stderr)
    sys.exit(5)
print("OK")
