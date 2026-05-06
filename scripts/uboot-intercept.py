#!/usr/bin/env python3
"""Hammer the UART with spaces during reset, surviving CH340 brownout.

The board's reset pulse causes the CH340 USB device to brown out for ~1s.
This script detects disconnect and re-opens the port as soon as it reappears,
then continues hammering. Total budget: --duration seconds wall-clock.

Press the reset button when the script tells you to.
"""
import argparse, os, sys, time, threading, serial, re

ap = argparse.ArgumentParser()
ap.add_argument('--port', default='/dev/ebaz-uart')
ap.add_argument('--baud', type=int, default=115200)
ap.add_argument('--duration', type=float, default=20.0)
args = ap.parse_args()

PROMPT_RE = re.compile(rb'(zynq[- ]?uboot>|U-Boot>|Zynq[- ]?>|=>\s*$)', re.IGNORECASE)
HAMMER = b'd'  # EBAZ4205 miner U-Boot uses 'd' as autoboot break key

state = {'ser': None, 'stop': False, 'prompt_hit': False, 'buf': b''}

def open_port():
    while not state['stop'] and not os.path.exists(args.port):
        time.sleep(0.05)
    if state['stop']:
        return None
    for _ in range(20):  # retry briefly while udev finishes binding
        try:
            return serial.Serial(args.port, args.baud, timeout=0.05)
        except (serial.SerialException, OSError):
            time.sleep(0.05)
    return None

def hammer_thread():
    while not state['stop']:
        s = state['ser']
        if s is not None:
            try:
                s.write(HAMMER)
            except Exception:
                pass
        time.sleep(0.03)

threading.Thread(target=hammer_thread, daemon=True).start()

state['ser'] = open_port()
if state['ser'] is None:
    print(f"could not open {args.port}", file=sys.stderr); sys.exit(1)
print(f"opened {args.port} @ {args.baud}", flush=True)
print(f"=== PRESS THE RESET BUTTON (S2 / 主板復位鍵) NOW — script will survive disconnect ===", flush=True)

deadline = time.time() + args.duration
reconnects = 0
try:
    while time.time() < deadline and not state['prompt_hit']:
        s = state['ser']
        if s is None:
            time.sleep(0.05)
            state['ser'] = open_port()
            if state['ser'] is not None:
                reconnects += 1
                sys.stdout.write(f"\n[reconnected #{reconnects}]\n"); sys.stdout.flush()
            continue
        try:
            data = s.read(2048)
        except (serial.SerialException, OSError) as e:
            sys.stdout.write(f"\n[disconnect: {e}]\n"); sys.stdout.flush()
            try: s.close()
            except: pass
            state['ser'] = None
            continue
        if not data:
            continue
        state['buf'] += data
        try:
            sys.stdout.write(data.decode('utf-8', errors='replace'))
        except Exception:
            sys.stdout.write(data.hex(' '))
        sys.stdout.flush()
        if PROMPT_RE.search(state['buf'][-200:]):
            state['prompt_hit'] = True
            sys.stdout.write("\n[*** U-Boot prompt detected ***]\n"); sys.stdout.flush()
finally:
    state['stop'] = True
    if state['ser']:
        try: state['ser'].close()
        except: pass
    print(f"\n--- received {len(state['buf'])} bytes, reconnects={reconnects}, prompt_detected={state['prompt_hit']} ---")
