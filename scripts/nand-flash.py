#!/usr/bin/env python3
"""Flash kernel + dtb + rootfs to EBAZ4205 NAND via U-Boot ymodem + nand cmds.

Assumes:
  - Board is at U-Boot prompt 'zynq-uboot>' on /dev/ebaz-uart @ 115200
  - lrzsz's `sb` is available on host (ymodem send)
  - Files to flash exist at the paths passed

Sequence per (file, nand_offset, partition_size):
  1. send `loady 0x4000000` to U-Boot
  2. wait for "Ready for binary" banner
  3. close UART, spawn `sb -k <file>` reading/writing /dev/ebaz-uart
  4. reopen UART, wait for "## Total Size" + 'zynq-uboot>' prompt
  5. send `nand erase <offset> <partition_size>` (full erase block alignment)
  6. send `nand write 0x4000000 <offset> <actual_file_size>`
"""
import argparse, os, subprocess, sys, time
import serial

DEFAULT_LOAD_ADDR = 0x04000000

# (name, path, nand_offset, partition_size_for_erase)
# Paths are joined with --buildroot (default: build/buildroot). Use `../..`
# to escape back to the repo root for non-buildroot artifacts like
# backup/top.bit.
LAYOUT = [
    ('uImage',    'output/images/uImage',            0x00300000, 0x00500000),
    ('dtb',       'output/images/zynq-ebaz4205.dtb', 0x00800000, 0x00020000),
    ('bitstream', '../../backup/top.bit',            0x02220000, 0x00800000),
    ('rootfs',    'output/images/rootfs.jffs2',      0x02a20000, 0x04000000),
]

PROMPT = b'zynq-uboot>'
READY_RE = b'Ready for binary'
LOADY_DONE_RE = b'## Total Size'

def wait_for(s, needle, timeout):
    """Read from serial until `needle` appears or timeout. Return collected bytes."""
    deadline = time.time() + timeout
    buf = b''
    while time.time() < deadline:
        chunk = s.read(2048)
        if chunk:
            buf += chunk
            sys.stdout.write(chunk.decode('utf-8', errors='replace'))
            sys.stdout.flush()
            if needle in buf:
                return buf
    raise TimeoutError(f"didn't see {needle!r} within {timeout}s; last buf tail: {buf[-200:]!r}")

def cmd(s, line, prompt_timeout=5):
    s.write(line.encode() + b'\r')
    return wait_for(s, PROMPT, prompt_timeout)

def round_up(value, align):
    return (value + align - 1) & ~(align - 1)

def flash_one(port, name, file_path, nand_offset, part_size, load_addr, page_size=2048):
    if not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)
    file_size = os.path.getsize(file_path)
    write_size = round_up(file_size, page_size)
    print(f"\n=== {name}: {file_path} ({file_size} bytes, will write {write_size}) ===", flush=True)
    if write_size > part_size:
        raise ValueError(f"file {file_size} > partition {part_size}")

    # Step 1+2: ask U-Boot to receive ymodem
    s = serial.Serial(port, 115200, timeout=0.2)
    s.reset_input_buffer()
    s.write(f"loady 0x{load_addr:08x}\r".encode())
    wait_for(s, READY_RE, timeout=5)
    s.close()

    # Step 3: sb sends the file via ymodem on /dev/ebaz-uart
    print(f"[*] running sb -k {file_path}", flush=True)
    with open(port, 'r+b', buffering=0) as tty, open('/tmp/sb-progress.log', 'wb') as log:
        rc = subprocess.run(['sb', '-k', file_path], stdin=tty, stdout=tty, stderr=log)
        if rc.returncode != 0:
            raise RuntimeError(f"sb failed rc={rc.returncode} (see /tmp/sb-progress.log)")

    # Step 4: re-open and consume ymodem complete + prompt
    s = serial.Serial(port, 115200, timeout=0.2)
    wait_for(s, PROMPT, timeout=15)

    # Step 5: erase the partition (must be erase-block aligned, miner uses 128 KB blocks)
    print(f"[*] nand erase 0x{nand_offset:08x} 0x{part_size:08x}", flush=True)
    cmd(s, f"nand erase 0x{nand_offset:08x} 0x{part_size:08x}", prompt_timeout=30)

    # Step 6: write
    print(f"[*] nand write 0x{load_addr:08x} 0x{nand_offset:08x} 0x{write_size:08x}", flush=True)
    cmd(s, f"nand write 0x{load_addr:08x} 0x{nand_offset:08x} 0x{write_size:08x}", prompt_timeout=60)

    s.close()
    print(f"=== {name} done ===", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ebaz-uart')
    ap.add_argument('--buildroot', default='/home/test/xilinx/build/buildroot')
    ap.add_argument('--only', help='comma-separated subset: uImage,dtb,rootfs')
    args = ap.parse_args()

    only = set(args.only.split(',')) if args.only else None
    for name, rel, off, sz in LAYOUT:
        if only and name not in only:
            continue
        flash_one(args.port, name, os.path.join(args.buildroot, rel), off, sz, DEFAULT_LOAD_ADDR)

    print("\nALL DONE — issue `reset` at U-Boot prompt to boot the new system.")


if __name__ == '__main__':
    main()
