#!/usr/bin/env python3
"""One-shot UART interaction over /dev/ebaz-uart.

Usage:
  uart-poke.py [--send <bytes>] [--wait <secs>] [--port <dev>] [--baud <n>]

  --send accepts python-style escapes: '\\r', '\\n', '\\x03' (Ctrl-C), etc.
  --send '' just listens.

Examples:
  uart-poke.py --send '\\r' --wait 3
  uart-poke.py --send 'root\\r' --wait 2
  uart-poke.py --send '' --wait 8       # passive listen
"""
import argparse, codecs, sys, time, serial

ap = argparse.ArgumentParser()
ap.add_argument('--port', default='/dev/ebaz-uart')
ap.add_argument('--baud', type=int, default=115200)
ap.add_argument('--send', default='')
ap.add_argument('--wait', type=float, default=3.0)
ap.add_argument('--quiet', action='store_true', help='suppress trailing hex dump')
args = ap.parse_args()

payload = codecs.decode(args.send, 'unicode_escape').encode('latin-1') if args.send else b''

s = serial.Serial(args.port, args.baud, timeout=0.1)
if payload:
    print(f">>> sending {payload!r}", flush=True)
    s.write(payload); s.flush()

deadline = time.time() + args.wait
total = 0
buf = b''
try:
    while time.time() < deadline:
        try:
            data = s.read(4096)
        except serial.SerialException as e:
            print(f"\n[serial error: {e}]", flush=True); break
        if data:
            total += len(data)
            buf += data
            try:
                sys.stdout.write(data.decode('utf-8', errors='replace'))
            except Exception:
                sys.stdout.write(data.hex(' '))
            sys.stdout.flush()
finally:
    print(f"\n--- got {total} bytes ---")
    if buf and total < 600 and not args.quiet:
        print("hex:", buf.hex(' '))
    s.close()
