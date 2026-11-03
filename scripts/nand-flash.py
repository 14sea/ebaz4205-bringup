#!/usr/bin/env python3
"""Flash kernel + dtb + rootfs to EBAZ4205 NAND via U-Boot ymodem + nand cmds.

Assumes:
  - Board is at U-Boot prompt 'zynq-uboot>' on /dev/ebaz-uart @ 115200
  - lrzsz's `sb` is available on host (ymodem send)
  - Files to flash exist at the paths passed

Sequence per (file, nand_offset, partition_size):
  0. `nand bad`: refuse if the bad blocks inside the partition leave too little room
     (`nand write` skips bad blocks, so the data would spill into the next partition)
  1. send `loady 0x4000000` to U-Boot
  2. wait for "Ready for binary" banner
  3. close UART, spawn `sb -k <file>` reading/writing /dev/ebaz-uart
  4. reopen UART, wait for "## Total Size" + 'zynq-uboot>' prompt
  4b. `crc32 <load> <file_size>` must equal the host CRC-32 before anything is erased
  5. send `nand erase <offset> <partition_size>` (full erase block alignment); expect OK
  6. send `nand write 0x4000000 <offset> <page-aligned size>`; expect "written: OK"
  7. `nand read` it back to a second RAM buffer and compare its crc32 with the host CRC

The board's U-Boot is 2015.07 (strings in backup/boot.bin): it has `crc32`
("... ==> %08lx") and `nand bad` ("Device %d bad blocks:" + "  %08llx" lines), and
reports nand read/write as " %zu bytes %s: %s" with OK/ERROR.
`--self-test` checks the parsers and the partition-room rule without a board.
"""
import argparse, os, re, subprocess, sys, time, zlib
import serial

DEFAULT_LOAD_ADDR = 0x04000000
ERASE_BLOCK = 0x20000      # W29N01HV: 128 KiB erase blocks
RAM_LIMIT = 0x0C000000     # keep load + readback buffers well below U-Boot's relocation

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

BAD_HDR_RE = re.compile(rb'bad blocks:')
BAD_LINE_RE = re.compile(rb'^\s+([0-9a-fA-F]{8,16})\s*$', re.M)
CRC_RE = re.compile(rb'==>\s*([0-9a-fA-F]{8})')

def parse_bad_blocks(buf):
    """Offsets listed by `nand bad` (only the lines after the 'bad blocks:' header)."""
    m = BAD_HDR_RE.search(buf)
    if not m:
        raise ValueError(f"no 'bad blocks:' header in `nand bad` output: {buf[-200:]!r}")
    return [int(x, 16) for x in BAD_LINE_RE.findall(buf[m.end():])]

def parse_crc32(buf):
    """Last 'xxxxxxxx' after '==>' in a `crc32` reply (the echoed command has no '==>')."""
    found = CRC_RE.findall(buf)
    if not found:
        raise ValueError(f"no crc32 result in: {buf[-200:]!r}")
    return int(found[-1], 16)

def usable_room(bad, nand_offset, part_size, block=ERASE_BLOCK):
    """Bytes `nand write` can place inside the partition, given its bad blocks."""
    inside = [b for b in bad if nand_offset <= b < nand_offset + part_size]
    return part_size - len(inside) * block, inside

def expect_ok(buf, what, needle=b'OK'):
    if b'ERROR' in buf or needle not in buf:
        raise RuntimeError(f"{what} did not report {needle!r}: {buf[-300:]!r}")

def flash_one(port, name, file_path, nand_offset, part_size, load_addr, page_size=2048):
    if not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)
    file_size = os.path.getsize(file_path)
    write_size = round_up(file_size, page_size)
    print(f"\n=== {name}: {file_path} ({file_size} bytes, will write {write_size}) ===", flush=True)
    if write_size > part_size:
        raise ValueError(f"file {file_size} > partition {part_size}")
    with open(file_path, 'rb') as f:
        host_crc = zlib.crc32(f.read()) & 0xFFFFFFFF
    readback_addr = load_addr + round_up(write_size, 0x100000)
    if readback_addr + write_size > RAM_LIMIT:
        raise ValueError(f"readback buffer 0x{readback_addr:08x}+0x{write_size:x} exceeds 0x{RAM_LIMIT:08x}")

    # Step 0: bad blocks inside the partition must leave room for the whole write
    s = serial.Serial(port, 115200, timeout=0.2)
    s.reset_input_buffer()
    s.write(b'\r')                         # flush a residual 'd' left by uboot-intercept.py
    wait_for(s, PROMPT, timeout=5)
    bad = parse_bad_blocks(cmd(s, "nand bad", prompt_timeout=10))
    room, inside = usable_room(bad, nand_offset, part_size)
    print(f"[*] bad blocks in partition: {[hex(b) for b in inside] or 'none'}; room 0x{room:x}, need 0x{write_size:x}", flush=True)
    if write_size > room:
        s.close()
        raise RuntimeError(f"{name}: {len(inside)} bad block(s) leave 0x{room:x} < 0x{write_size:x}; "
                           f"nand write would spill past 0x{nand_offset + part_size:08x}. Nothing written.")

    # Step 1+2: ask U-Boot to receive ymodem
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

    # Step 4b: the transfer must be intact before anything is erased
    ram_crc = parse_crc32(cmd(s, f"crc32 0x{load_addr:08x} 0x{file_size:x}", prompt_timeout=20))
    if ram_crc != host_crc:
        s.close()
        raise RuntimeError(f"{name}: RAM crc32 {ram_crc:08x} != host {host_crc:08x} after ymodem. Nothing erased.")
    print(f"[*] RAM crc32 {ram_crc:08x} == host", flush=True)

    # Step 5: erase the partition (must be erase-block aligned, miner uses 128 KB blocks)
    print(f"[*] nand erase 0x{nand_offset:08x} 0x{part_size:08x}", flush=True)
    expect_ok(cmd(s, f"nand erase 0x{nand_offset:08x} 0x{part_size:08x}", prompt_timeout=30), "nand erase")

    # Step 6: write
    print(f"[*] nand write 0x{load_addr:08x} 0x{nand_offset:08x} 0x{write_size:08x}", flush=True)
    expect_ok(cmd(s, f"nand write 0x{load_addr:08x} 0x{nand_offset:08x} 0x{write_size:08x}", prompt_timeout=60),
              "nand write", b'written: OK')

    # Step 7: read back into a second buffer and compare
    expect_ok(cmd(s, f"nand read 0x{readback_addr:08x} 0x{nand_offset:08x} 0x{write_size:08x}", prompt_timeout=120),
              "nand read", b'read: OK')
    nand_crc = parse_crc32(cmd(s, f"crc32 0x{readback_addr:08x} 0x{file_size:x}", prompt_timeout=20))
    s.close()
    if nand_crc != host_crc:
        raise RuntimeError(f"{name}: NAND readback crc32 {nand_crc:08x} != host {host_crc:08x}. "
                           f"The partition holds a bad image — do not reset into it.")
    print(f"[*] NAND readback crc32 {nand_crc:08x} == host", flush=True)
    print(f"=== {name} done ===", flush=True)


def self_test():
    """Board-free checks of the parsers and the room rule (U-Boot 2015.07 output shapes)."""
    bad_out = b"nand bad\r\n\r\nDevice 0 bad blocks:\r\n  02240000\r\n  06a40000\r\nzynq-uboot> "
    assert parse_bad_blocks(bad_out) == [0x02240000, 0x06a40000]
    assert parse_bad_blocks(b"nand bad\r\n\r\nDevice 0 bad blocks:\r\nzynq-uboot> ") == []
    try:
        parse_bad_blocks(b"Unknown command 'nand'\r\nzynq-uboot> ")
        raise AssertionError("missing header accepted")
    except ValueError:
        pass
    crc_out = b"crc32 0x04000000 0x4d1a50\r\nCRC32 for 04000000 ... 044d1a4f ==> 1a2b3c4d\r\nzynq-uboot> "
    assert parse_crc32(crc_out) == 0x1a2b3c4d
    try:
        parse_crc32(b"crc32 0x04000000 0x10\r\nzynq-uboot> ")
        raise AssertionError("missing crc accepted")
    except ValueError:
        pass
    # uImage partition: 40 blocks; a 5,052,880-byte image needs 39 -> one bad block fits, two do not
    off, size, need = 0x00300000, 0x00500000, round_up(5052880, 2048)
    assert usable_room([], off, size)[0] == size
    assert usable_room([0x00320000], off, size)[0] >= need
    assert usable_room([0x00320000, 0x00340000], off, size)[0] < need
    assert usable_room([0x00800000], off, size)[1] == []          # next partition's block is not counted
    expect_ok(b" 5052416 bytes written: OK\r\n", "w", b'written: OK')
    for bad_reply in (b" 5052416 bytes written: ERROR\r\n", b"Erasing at 0x7e0000 -- 100% complete.\r\nERROR\r\n"):
        try:
            expect_ok(bad_reply, "x", b'written: OK' if b'written' in bad_reply else b'OK')
            raise AssertionError("error reply accepted")
        except RuntimeError:
            pass
    print("self-test PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ebaz-uart')
    ap.add_argument('--buildroot', default='/home/test/xilinx/build/buildroot')
    ap.add_argument('--only', help='comma-separated subset: uImage,dtb,bitstream,rootfs')
    ap.add_argument('--self-test', action='store_true', help='check parsers and the room rule, no board')
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    only = set(args.only.split(',')) if args.only else None
    for name, rel, off, sz in LAYOUT:
        if only and name not in only:
            continue
        flash_one(args.port, name, os.path.join(args.buildroot, rel), off, sz, DEFAULT_LOAD_ADDR)

    print("\nALL DONE — issue `reset` at U-Boot prompt to boot the new system.")


if __name__ == '__main__':
    main()
