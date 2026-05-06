#!/usr/bin/env python3
"""Push a binary file from host to the board via base64 over the UART shell.

Board side (Linux): runs
   head -c <B> > <tmp_b64>; base64 -d <tmp_b64> > <dest> && md5sum <dest>
where <B> is the size in bytes of the base64 text. Host then writes exactly
that many bytes of base64 text to /dev/ebaz-uart.

Why head -c instead of cat: cat needs an explicit EOF (Ctrl-D), which is
easy to get wrong over a raw serial line.

Usage:
  uart-push-b64.py --in top.bit --dest /tmp/top.bit
"""
import argparse, base64, hashlib, os, sys, time
import serial

ap = argparse.ArgumentParser()
ap.add_argument('--port', default='/dev/ebaz-uart')
ap.add_argument('--baud', type=int, default=115200)
ap.add_argument('--in', dest='infile', required=True)
ap.add_argument('--dest', required=True, help='destination path on board')
ap.add_argument('--tmp', default='/tmp/_push.b64',
                help='intermediate base64 file path on board')
ap.add_argument('--timeout', type=float, default=600.0)
args = ap.parse_args()

with open(args.infile, 'rb') as f:
    raw = f.read()
expected_md5 = hashlib.md5(raw).hexdigest()
_b64_raw = base64.b64encode(raw)
# Wrap to 76-char lines: matches the `base64` utility default. The board's
# shell runs in canonical (cooked) mode, so input must contain newlines or
# the tty line buffer (~4 KB) drops everything until one shows up.
LINE = 76
parts = [_b64_raw[i:i + LINE] for i in range(0, len(_b64_raw), LINE)]
b64 = b'\n'.join(parts) + b'\n'
b64_size = len(b64)
# Force line-buffered stdout so progress is visible in real time when this
# script runs as a background harness task (otherwise stdout is fully
# buffered to a pipe and we see nothing until exit).
sys.stdout.reconfigure(line_buffering=True)

print(f"file: {args.infile} -> {args.dest}")
print(f"raw={len(raw)} b64={b64_size} md5={expected_md5}")

s = serial.Serial(args.port, args.baud, timeout=0.2)
s.reset_input_buffer()

# Compose one shell line. Use a unique sentinel so we can find the md5 line.
#
# `stty -echo` is critical: without it, the board's tty canonical-mode echo
# bounces every base64 byte back to host (~2.8 MB of useless duplication for
# our payload), AND the kernel interleaves those echoed bytes with the
# `md5sum` / `echo` output that arrives later — making the trailing portion
# of the buffer unparseable. With echo off, the only board → host bytes
# after our typed-cmd line are the md5 line, the sentinel, and the prompt.
SENTINEL = "PUSH_DONE_" + str(int(time.time()))
cmd = (
    f"stty -echo; "
    f"head -c {b64_size} > {args.tmp}; "
    f"stty echo; "
    f"base64 -d {args.tmp} > {args.dest} && "
    f"md5sum {args.dest} && "
    f"echo {SENTINEL}\r"
)
s.write(cmd.encode())
s.flush()

# Brief settle so head -c is actually waiting on stdin.
# 200 ms is plenty over 115200 baud.
time.sleep(0.4)

# Now stream the b64 payload. The board's tty echoes every byte back to us
# (canonical mode echo is on for the login shell), so for a 2 MB payload we
# get 2 MB of echo plus prompt-side output. The kernel's USB-serial RX ring
# buffer (~16 KB) overflows long before we finish, dropping the LATER bytes —
# i.e. the md5sum + sentinel + prompt we actually care about. Drain the echo
# in lockstep with each write to keep the buffer below the overflow point.
chunk = 4096
written = 0
echo_drained = 0
t0 = time.time()
while written < b64_size:
    n = s.write(b64[written:written + chunk])
    s.flush()
    written += n
    # Drain echo. Read whatever the kernel has buffered right now (non-blocking
    # via in_waiting) so we never let it grow past a few KB.
    waiting = s.in_waiting
    if waiting:
        s.read(waiting)
        echo_drained += waiting
    if written % (256 * 1024) < chunk:
        elapsed = time.time() - t0
        rate = written / elapsed if elapsed else 0
        print(f"  tx={written}/{b64_size} drained={echo_drained} "
              f"({rate/1024:.1f} KB/s)")
print(f"  tx complete: {written} bytes in {time.time()-t0:.1f}s "
      f"(echo drained: {echo_drained})")

# The sentinel string appears TWICE in the stream:
#   (1) the shell's echo of the typed command line (mid-line, no prompt after)
#   (2) the actual `echo SENTINEL` after every other step succeeded, which the
#       shell follows with `\r\n# ` (newline + next prompt)
# Wait for (2) by looking for `SENTINEL\r\n# `. That sequence cannot appear
# anywhere else: '#' is not in the base64 alphabet, and the typed-line echo
# never has `# ` directly after the sentinel (more text follows).
TERMINATOR = (SENTINEL + "\r\n# ").encode()
# Looser fallback: match SENTINEL on its own line followed by a `#` somewhere
# soon after, to catch board variants that emit the prompt slightly differently.
LOOSE = (SENTINEL + "\r\n").encode()
deadline = time.time() + args.timeout
buf = bytearray()
last_log = time.time()
while time.time() < deadline:
    chunk = s.read(8192)
    if chunk:
        buf.extend(chunk)
        if TERMINATOR in buf:
            break
    if time.time() - last_log >= 30:
        print(f"  rx buffer = {len(buf)} bytes, still waiting for terminator")
        last_log = time.time()
else:
    # Timed out — save buffer so we can see what actually came back.
    dbg = '/tmp/uart-push-rx.bin'
    with open(dbg, 'wb') as f:
        f.write(buf)
    print(f"timeout waiting for terminator — rx={len(buf)} saved to {dbg}",
          file=sys.stderr)
    sys.exit(6)

s.close()

# Always save the post-TX RX buffer for debug — invaluable when terminator
# detection fails.
dbg = '/tmp/uart-push-rx.bin'
with open(dbg, 'wb') as f:
    f.write(buf)
print(f"  rx total = {len(buf)} bytes (saved {dbg})")

text = buf.decode('utf-8', errors='replace')
term_idx = text.find(SENTINEL + "\r\n# ")
if term_idx < 0:
    # Fall back to looser match: sentinel on its own line. Trade verification
    # rigor for not getting stuck.
    term_idx = text.find(SENTINEL + "\r\n")
    if term_idx < 0:
        print("no completion terminator — check the board manually",
              file=sys.stderr)
        sys.exit(2)

# md5sum's line is right before the terminator. Walk lines backward.
preamble = text[:term_idx]
got_md5 = None
for line in reversed(preamble.splitlines()):
    line = line.strip()
    if len(line) >= 32 and all(c in '0123456789abcdef' for c in line[:32]):
        got_md5 = line[:32]
        break

if got_md5 is None:
    print("could not find md5sum line in board output", file=sys.stderr)
    sys.exit(3)

print(f"board md5: {got_md5}")
if got_md5 != expected_md5:
    print(f"MD5 MISMATCH: expected {expected_md5}", file=sys.stderr)
    sys.exit(4)
print("OK")
