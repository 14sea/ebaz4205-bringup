# ebaz4205-bringup

Bringing custom Linux up on an **EBAZ4205** — a Zynq-7010 control board pulled from an Ebang Bitcoin miner. Goal: replace kernel, dtb, and rootfs with our own Buildroot-built image while leaving the original FSBL and U-Boot in NAND as a permanent recovery anchor.

This is a hardware bring-up workspace, not a software project. Most of the "code" is shell / Python plumbing around `openocd`, `u-boot`, and `buildroot`. For the deep details (NAND map, hardware quirks, recipes, traps) read [`CLAUDE.md`](CLAUDE.md) — that file is the canonical handbook.

## Hardware

- **SoC**: Xilinx XC7Z010-1CLG400I — dual Cortex-A9 + Artix-7 PL
- **NAND**: Winbond W29N01HV, 128 MiB (2 KiB pages, 128 KiB erase blocks), 9 partitions, miner BOOT.BIN at offset 0
- **JTAG**: Digilent HS3 (FT232H) on header J8 — `nSRST` not wired, so reset goes through SLCR writes
- **UART**: USB-CH340 on the V2.0 expansion board, wired to MIO of UART1 (`/dev/ttyPS0`), 115200 8N1
- **Ethernet**: dead at the magnetics; UART is the only interface in
- **Boot mode**: hard-strapped to NAND boot; no DIP switch on this PCB rev

## What works today

- ✅ Buildroot 2026.05 + Linux 6.12 booting from NAND (kernel + dtb + jffs2 rootfs)
- ✅ U-Boot recovery via SLCR soft-reset (no power cycle needed under normal conditions)
- ✅ FPGA dynamic reconfig via `fpgautil` (full reload, ~38 ms): runtime bitstream from `/dev/mtd5`, sliced from the original BOOT.BIN at byte `0x19770`
- ✅ Survives Type-C power cycle: mtd5 retains the bitstream, on-board reload via `dd` + `fpgautil` is one-shot
- ✅ Host ↔ board file transfer over UART via base64 (no network, no `lrzsz` on board needed)

## What doesn't (and why)

- **Network bring-up** — RJ45 magnetics are physically dead on this board, swap-tested. UART is the only path in
- **Partial reconfig (PL B-route)** — needs Vivado, no disk space for the install; deferred
- **Mainline U-Boot** — boots but has a UART pinmux conflict with the miner's MIO routing; staying on the original miner U-Boot, which works perfectly

## Repo layout

```
ebaz4205.cfg                   OpenOCD top-level config (HS3 + zynq_7000)
scripts/                       Bring-up plumbing
  jtag-scan.sh                   smoke-test the JTAG chain (IDCODE check)
  halt-cpu0.sh                   halt Cortex-A9 core 0 and dump registers
  program-pl.sh                  one-shot JTAG bitstream load (pld load)
  uboot-intercept.py             hammer 'd' on UART to break autoboot
  uart-poke.py                   universal UART scratchpad
  nand-flash.py                  ymodem + nand erase/write driver
  uart-capture-b64.py            board → host file transfer over UART
  uart-push-b64.py               host → board file transfer over UART
buildroot-config/              Buildroot defconfig — drop into buildroot/configs/
projects/                      reserved for FPGA project sources
bitstreams/                    reserved for built bitstreams
CLAUDE.md                      canonical handbook (read this!)
```

## Quick start

You need: a working JTAG + UART connection, the WSL/Linux host with `openocd`, `gh`, Python 3.12, and Buildroot 2026.05 (or compatible) checked out separately.

```bash
# 1. Set up Buildroot (one-time)
git clone https://gitlab.com/buildroot.org/buildroot.git build/buildroot
cp buildroot-config/ebaz4205_defconfig build/buildroot/configs/

# 2. Bring up Python venv (uart scripts depend on pyserial)
python3 -m venv .env
.env/bin/pip install pyserial pyftdi pyusb construct

# 3. Build kernel + rootfs
cd build/buildroot
PATH=$(echo "$PATH" | tr ':' '\n' | grep -v ' \| ' | grep -v '^/mnt/c' | tr '\n' ':' | sed 's/:$//')
PATH="$PATH" make ebaz4205_defconfig
PATH="$PATH" make BR2_JLEVEL=$(nproc) -j$(nproc)

# 4. Get to U-Boot prompt (background hammer + foreground SLCR reset)
cd ../..
.env/bin/python scripts/uboot-intercept.py --duration 25 &
sleep 1
openocd -f ebaz4205.cfg \
  -c "init" -c "halt" \
  -c "mww phys 0xF8000008 0xDF0D" \
  -c "mww phys 0xF8000200 1" \
  -c "shutdown"
wait

# 5. Flash and reboot
.env/bin/python scripts/nand-flash.py                  # all of uImage, dtb, rootfs
.env/bin/python scripts/uart-poke.py --send 'reset\r' --wait 60
```

For the FPGA reload workflow (`fpgautil` from mtd5), see the recipes section in [`CLAUDE.md`](CLAUDE.md).

## NAND layout

```
0x00000000   3 MB   nand-fsbl-uboot   original BOOT.BIN — never touch
0x00300000   5 MB   nand-linux        uImage
0x00800000 128 KB   nand-device-tree  zynq-ebaz4205.dtb
0x00820000  10 MB   nand-rootfs
0x01220000  16 MB   nand-jffs2
0x02220000   8 MB   nand-bitstream    PL bitstream for fpgautil reload
0x02a20000  64 MB   nand-allrootfs    mtdblock6 — active rootfs
0x06a20000  20 MB   nand-release
0x07e00000   2 MB   nand-reserve
```

## Status

Active solo project; expect `CLAUDE.md` to be the up-to-date source of truth as the workspace evolves. Pull requests welcome but the hardware-specific paths (NAND offsets, UART numbering, board photos) are tied to one specific PCB revision — mileage will vary.

## License

Code in this repository is MIT-licensed. Vendor reference materials (Xilinx FSBL, Ebang miner BOOT.BIN, schematics) are intentionally **not** included; bring your own.
