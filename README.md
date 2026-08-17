# ebaz4205-bringup

Bringing custom Linux up on an **EBAZ4205** — a Zynq-7010 control board pulled from an Ebang Bitcoin miner. Goal: replace kernel, dtb, and rootfs with our own Buildroot-built image while leaving the original FSBL and U-Boot in NAND as a permanent recovery anchor.

This is a hardware bring-up workspace, not a software project. Most of the "code" is shell / Python plumbing around `openocd`, `u-boot`, and `buildroot`. For the deep details (NAND map, hardware quirks, recipes, traps) read [`CLAUDE.md`](CLAUDE.md) — that file is the canonical handbook.

## Hardware

- **SoC**: Xilinx XC7Z010-1CLG400I — dual Cortex-A9 + Artix-7 PL
- **NAND**: Winbond W29N01HV, 128 MiB (2 KiB pages, 128 KiB erase blocks), 9 partitions, miner BOOT.BIN at offset 0
- **JTAG**: Digilent HS3 (FT232H) on header J8 — `nSRST` not wired, so reset goes through SLCR writes
- **UART**: USB-CH340 on the V2.0 expansion board, wired to MIO of UART1 (`/dev/ttyPS0`), 115200 8N1
- **Ethernet**: PHY (IP101G) is wired to **PL fabric pins**, not PS MIO — PS GEM0 reaches it over EMIO + FCLK3 (25 MHz) through the bitstream, so MDIO/link only work with a PL image loaded. On this particular board the port never links; see [What doesn't](#what-doesnt-and-why)
- **Boot mode**: hard-strapped to NAND boot; no DIP switch on this PCB rev

## What works today

- ✅ Buildroot 2026.05 + Xilinx `linux-xlnx` 6.12.70 booting from NAND (kernel + dtb + jffs2 rootfs)
- ✅ U-Boot recovery via SLCR soft-reset (no power cycle needed under normal conditions)
- ✅ FPGA dynamic reconfig via `fpgautil` (full reload, ~38 ms), using the PL bitstream carved out of the original miner BOOT.BIN
- ✅ Survives Type-C power cycle: the bitstream lives in NAND, so the on-board reload is `dd` + `fpgautil`, no host involvement
- ✅ Host ↔ board file transfer over UART via base64 (no network, no `lrzsz` on board needed)
- ✅ Console login is `root` with an empty password (send `root\r` at `buildroot login:` — one CR, not two, or getty eats the second)

The bitstream slice lives in mtd0 (the untouched miner BOOT.BIN) at byte `0x19770`, length `0x1FCB70`:

```sh
dd if=/dev/mtd0 bs=16 skip=6519 count=130231 of=/tmp/top.bit   # md5 2d68aabf05b260779958e7f741bc0988
fpgautil -b /tmp/top.bit                                        # ~38 ms, state=operating
```

A copy was also written to mtd5 (`nand-bitstream`) via `nand-flash.py --only bitstream`, but a later re-read
found mtd5 blank at offset 0 — until that is re-verified, **treat mtd0 @ `0x19770` as the source of truth**.

## What doesn't (and why)

- **Network bring-up** — the RJ45 port has never linked on this board. The PHY itself is provably fine: read
  over GEM0 `phy_maint` it returns PHYID `0x02430C54`, BMCR `0x3100`, BMSR `0x7849` (link=0), **LPA `0x0000`**,
  and FCLK3 measures 25.0 MHz — i.e. clock, bitstream routing and MDIO are all intact and the PHY simply
  receives nothing, so the break is downstream on the copper (magnetics / jack / MDI traces). Reflowing the
  BT16B03 jack pins did not help. Caveat worth knowing before spending money on it: a *brand-new* EBAZ4203
  produced the byte-identical "dead" signature when cabled straight to a PC NIC, and linked instantly on a
  router LAN port — so this signature is also what a peer-side false negative looks like. A router retest of
  the 4205 still failed while four 4203s passed on the same router in the same session, but the exact cable
  was never cross-checked back-to-back. Verdict: **provisionally condemned, repair not attempted further**.
  UART is the only path in
- **Partial reconfig (PL B-route)** — needs Vivado, no disk space for the install; deferred
- **Mainline U-Boot** — boots but has a UART pinmux conflict with the miner's MIO routing; staying on the original miner U-Boot, which works perfectly
- **USB gadget (`g_ether`) as a network substitute** — impossible, there is no USB hardware on the board: the PS ULPI PHY and connector are simply not populated (the only "USB" is the CH340 serial bridge on the expansion board)

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
buildroot/                     source of truth for the Buildroot side — mirror into build/buildroot/
  configs/ebaz4205_defconfig     the defconfig
  board/ebaz4205/patches/        BR2_GLOBAL_PATCH_DIR — kernel patches (fclk-enable=<9>)
projects/                      reserved for FPGA project sources
bitstreams/                    reserved for built bitstreams
CLAUDE.md                      canonical handbook (read this!)
```

Not in git (see `.gitignore`): `build/` (Buildroot checkout, its own repo), `.env/` (Python venv —
a directory, not a dotenv file), `backup/` and `references/`/`img/`/`*.pdf` (vendor BOOT.BIN,
bitstream and schematics — not redistributable, bring your own).

## Quick start

You need: a working JTAG + UART connection, a Linux/WSL host with `openocd`, `lrzsz` (`sb` for the ymodem
upload), Python 3.12, and a Buildroot 2026.05-or-later checkout of your own.

```bash
# 0. Every script defaults to the /dev/ebaz-uart symlink — the raw /dev/ttyUSBN number is
#    unstable across the brownout that a board reset causes. Create it once:
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="ebaz-uart"' \
  | sudo tee /etc/udev/rules.d/90-ebaz-uart.rules
sudo udevadm control --reload-rules && sudo udevadm trigger

# 1. Set up Buildroot (one-time). Mirror BOTH the defconfig AND the board patch
#    dir — the defconfig sets BR2_GLOBAL_PATCH_DIR="board/ebaz4205/patches", which
#    applies the fclk-enable=<9> dts fix (without it, PL AXI hangs Linux hard).
git clone https://gitlab.com/buildroot.org/buildroot.git build/buildroot
cp -r buildroot/configs buildroot/board build/buildroot/

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

# 5. Clear the residual 'd' the intercept left in U-Boot's input buffer, then flash and reboot.
#    Without the bare CR the next command arrives as `ddloady ...` -> "Unknown command".
.env/bin/python scripts/uart-poke.py --send '\r' --wait 2 --quiet
.env/bin/python scripts/nand-flash.py                  # all of uImage, dtb, rootfs
.env/bin/python scripts/uart-poke.py --send 'reset\r' --wait 60
```

Two things that will bite you on the Buildroot side: a plain `make` after enabling a new package
only re-runs the rootfs assembly stage (force it with `make <pkgname>` first), and changing a
patch under `board/ebaz4205/patches/` needs `make linux-dirclean && make linux` to re-extract.

For the FPGA reload workflow and the full trap list, see the recipes section in [`CLAUDE.md`](CLAUDE.md).

## NAND layout

```
0x00000000   3 MB   nand-fsbl-uboot   original BOOT.BIN — never touch
0x00300000   5 MB   nand-linux        uImage
0x00800000 128 KB   nand-device-tree  zynq-ebaz4205.dtb
0x00820000  10 MB   nand-rootfs
0x01220000  16 MB   nand-jffs2
0x02220000   8 MB   nand-bitstream    mtd5 — intended home for the fpgautil bitstream (see caveat above)
0x02a20000  64 MB   nand-allrootfs    mtdblock6 — active rootfs
0x06a20000  20 MB   nand-release
0x07e00000   2 MB   nand-reserve
```

## Status

The bring-up itself is done and stable — kernel/dtb/rootfs iteration, U-Boot recovery and PL reload
all work, and the board now mostly serves as the hardware platform for follow-on FPGA experiments
kept in their own repositories. This repo gets updated when the bring-up flow itself changes;
`CLAUDE.md` is the up-to-date source of truth in between.

Solo hobby project. Pull requests welcome, but the hardware-specific bits (NAND offsets, UART
routing, expansion-board wiring) are tied to one specific PCB revision — mileage will vary.

## License

Code in this repository is MIT-licensed. Vendor reference materials (Xilinx FSBL, Ebang miner BOOT.BIN, schematics) are intentionally **not** included; bring your own.
