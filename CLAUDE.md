# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this workspace is

This is a working directory for bringing up a custom Linux on an **EBAZ4205** — an Ebang Bitcoin-miner control board built around an Xilinx **XC7Z010-1CLG400I** (Zynq-7000, dual Cortex-A9 + Artix-7 PL). The board ships with a stripped-down PetaLinux miner image in NAND. The goal here is replacing kernel + dtb + rootfs while keeping the original BOOT.BIN/FSBL/U-Boot intact, so the system can always recover via JTAG.

It's not a software project — it's a hardware bring-up workspace. Most "code" is shell/TCL/Python plumbing around openocd, U-Boot, and Buildroot.

## Hardware setup (essential context)

Without knowing this physical setup, none of the scripts make sense:

- **JTAG**: Digilent HS3 (FT232H, USB `0403:6014`, serial `210299769260`) on the 14-pin J8 header. SRST is **not wired**, so HS3's `nSRST` does nothing — reset must go through SLCR writes.
- **UART**: A Digilent USB-CH340 on the V2.0 expansion board, `1a86:7523`, at 115200 8N1. Wires go from expansion P5 (TXD/RXD) to main board J7 → MIO ball A16/F15. **This is UART1's MIO mux**, not UART0. The Linux/U-Boot console is `/dev/ttyPS0` which on this board's miner config maps to the UART1 controller at `0xE0001000` (UART0 at `0xE0000000` is dead). Scripts use `/dev/ebaz-uart` symlink (created by `/etc/udev/rules.d/90-ebaz-uart.rules`); the underlying `/dev/ttyUSBN` number is unstable across detach/reattach and gets ghost-stuck after CH340 brownout, so always reference the symlink.
- **Power**: Single Type-C on the V2.0 board powers the whole stack. Resets cause a brief 3.3V brownout that drops CH340 from WSL's usbipd — scripts must tolerate `/dev/ttyUSB0` disappearing for ~1s.
- **Ethernet is dead — but the PHY is healthy; the fault is the magnetics/RJ45 jack (BT16B03) or its solder.** The Ethernet PHY (IC Plus IP101G) is wired to **PL fabric pins**, not PS MIO — PS GEM0 reaches it via EMIO + FCLK3(25 MHz) through the PL bitstream (100 Mbps MII). Seller's reference Vivado project is in `references/13_Ethernet/` (pinout: `…/project_1.srcs/constrs_1/new/pin.xdc`). Register-level proof (2026-06-10, read via GEM phy_maint `0xE000B034` + `devmem`, addr 0): PHYID `0x02430C54`, BMCR `0x3100` (autoneg/100M-FD/not power-down/not isolate), BMSR `0x7849` (link=0, autoneg-incomplete), **LPA `0x0000` / ANER `0x0004`** → with a known-good cable to a powered PC the PHY receives nothing. FCLK3 confirmed = 25.0 MHz (SLCR `0xF8000180`=`0x00500800`, IOPLL 1000 MHz). So the whole PS→PHY chain (bitstream routing + clock + MDIO) is intact — the break is *downstream of the PHY* on the copper. NOT a software/bitstream/clk problem. "Worked at purchase, dead after disuse" fits oxidized RJ45 contacts / cracked-cold solder → reflow the 8 jack pins or replace BT16B03. Until physically repaired, UART is the only way in.
- **Boot mode DIP**: This PCB rev has **no DIP switch** — boot mode is fixed at NAND boot via solder straps. JTAG-boot is not reachable without soldering.
- **Original U-Boot's autoboot break key is `'d'`** (not Space/Ctrl-C). Hammering anything else won't interrupt it.

## NAND layout (memorize)

Confirmed from the original miner kernel's mtdparts (9 partitions, NAND is Winbond W29N01HV, 128 MiB, 2 KiB pages, 128 KiB erase blocks):

```
0x00000000  3 MB    nand-fsbl-uboot   ← original BOOT.BIN, NEVER touch
0x00300000  5 MB    nand-linux        ← uImage (we overwrite)
0x00800000  128 KB  nand-device-tree  ← dtb     (we overwrite)
0x00820000  10 MB   nand-rootfs
0x01220000  16 MB   nand-jffs2
0x02220000  8 MB    nand-bitstream    ← FPGA PL bitstream
0x02a20000  64 MB   nand-allrootfs    ← mtdblock6 = active rootfs (we overwrite)
0x06a20000  20 MB   nand-release
0x07e00000  2 MB    nand-reserve
```

Original `bootargs`: `console=ttyPS0,115200 root=/dev/mtdblock6 rootfstype=jffs2 noinitrd rw rootwait`

## Recipes

**Smoke-test JTAG chain** (expects IDCODE `0x13722093` for XC7Z010 + `0x4ba00477` for ARM DAP):
```bash
scripts/jtag-scan.sh
```

**Get to U-Boot prompt from any board state**:
```bash
# Background: hammer 'd' on UART
.env/bin/python scripts/uboot-intercept.py --duration 25 &
sleep 1
# Foreground: SLCR-unlock + PSS_RST_CTRL=1 triggers a full PS soft reset
openocd -f ebaz4205.cfg \
  -c "init" -c "halt" \
  -c "mww phys 0xF8000008 0xDF0D" \
  -c "mww phys 0xF8000200 1" \
  -c "shutdown"
wait
```
Look for `[*** U-Boot prompt detected ***]`. Leaves `zynq-uboot>` waiting for input on `/dev/ebaz-uart`.

**Build kernel + rootfs**:
```bash
# build/buildroot/ is its OWN git checkout (separate .git) and is .gitignored here,
# so the source-of-truth defconfig + board patches live tracked under buildroot/ and
# get mirrored in before building:
cp -r buildroot/configs buildroot/board build/buildroot/
cd build/buildroot
# Strip PATH of Windows entries — Buildroot rejects PATH with spaces (WSL inherits Windows PATH)
PATH=$(echo "$PATH" | tr ':' '\n' | grep -v ' \| ' | grep -v '^/mnt/c' | tr '\n' ':' | sed 's/:$//')
PATH="$PATH" make ebaz4205_defconfig
PATH="$PATH" make BR2_JLEVEL=20 -j20
```
The defconfig sets `BR2_GLOBAL_PATCH_DIR="board/ebaz4205/patches"`, which applies
`buildroot/board/ebaz4205/patches/linux/*.patch` to the kernel at extract time (currently
the `fclk-enable=<9>` dts fix — see "Things that bit us"). Applying a new/changed patch
needs a re-extract: `make linux-dirclean && make linux`.
Outputs land in `build/buildroot/output/images/`: `uImage`, `zynq-ebaz4205.dtb`, `rootfs.jffs2`.

**Flash kernel/dtb/rootfs to NAND** (requires U-Boot prompt active):
```bash
.env/bin/python scripts/nand-flash.py            # all three
.env/bin/python scripts/nand-flash.py --only dtb # subset: uImage,dtb,rootfs
```

**Iterate (rootfs-only changes)**:
```bash
cd build/buildroot && PATH=... make -j20    # rebuild changes
# get U-Boot prompt as above
.env/bin/python scripts/nand-flash.py --only rootfs
.env/bin/python scripts/uart-poke.py --send 'reset\r' --wait 60
```

**FPGA full-reload via fpgautil (A-route)**: requires `BR2_PACKAGE_XILINX_FPGAUTIL=y` in defconfig and a built rootfs that includes `/usr/bin/fpgautil`. Bitstream-of-truth lives in mtd0 BOOT.BIN starting at byte 0x19770 (length 0x1FCB70). The 2 MB slice is in `backup/top.bit` and persisted to mtd5 (nand-bitstream). Steps from a running Linux:
```bash
# one-time bootstrap to capture the bitstream from mtd0 to host (slow: ~6 min):
.env/bin/python scripts/uart-capture-b64.py \
  --out backup/boot.bin --expect 3145728 \
  --md5 3cf9aa8182714b63d35e1b2d2a619026 \
  --send 'dd if=/dev/mtd0 of=/tmp/boot.bin bs=4096 2>/dev/null; base64 /tmp/boot.bin'
# slice on host (skip 0x19770, take 0x1FCB70 — sync word 66 55 99 aa):
python3 -c "d=open('backup/boot.bin','rb').read(); open('backup/top.bit','wb').write(d[0x19770:0x19770+0x1FCB70])"
# write to mtd5 once via U-Boot (survives reboots):
# get U-Boot prompt as above
.env/bin/python scripts/nand-flash.py --only bitstream
.env/bin/python scripts/uart-poke.py --send 'reset\r' --wait 60
# from now on, on every fresh boot, runtime reload is just:
#   dd if=/dev/mtd5 bs=4096 count=509 of=/tmp/top.bit
#   fpgautil -b /tmp/top.bit
```

## Key script invariants

- **`scripts/uart-poke.py`** is the universal UART scratchpad. `--send` accepts Python escapes (`\r`, `\x03`). `--wait 0 --send ''` to passive-listen. `--quiet` suppresses the trailing hex dump (use it whenever the output volume matters — e.g. bring-up scripts piped through harness/agent layers).
- **`scripts/uboot-intercept.py`** must be tolerant of `/dev/ttyUSB0` disappearing mid-run (CH340 brownout). It reopens the port until `--duration` elapses or it sees a known U-Boot prompt regex.
- **`scripts/nand-flash.py`** sequence per file: `loady 0x4000000` → `sb -k <file>` (host writes ymodem to `/dev/ebaz-uart`, stderr captured to `/tmp/sb-progress.log`) → `nand erase <off> <part_size>` → `nand write 0x4000000 <off> <page-aligned size>`. Erase always covers the full partition; write only the actual file size (page-aligned). `LAYOUT` constant in the file is the source of truth for offsets — currently includes `uImage`, `dtb`, `bitstream` (→ mtd5), `rootfs`. Paths are joined with `--buildroot` (default `build/buildroot`); the bitstream entry uses `../../backup/top.bit` to escape back to the repo root.
- **`scripts/uart-capture-b64.py`** pulls a binary file from a running Linux on the board to the host over UART. Board side runs e.g. `base64 /tmp/boot.bin`; host script frames with `BEGIN_B64` / `END_B64` markers and waits for `END_B64\r\n# ` (sentinel + prompt) so a coincidental match in command echo doesn't trip it. Decoded body is filtered through a strict base64 alphabet whitelist before decode. Saves `<out>.raw` alongside `<out>` for post-mortem.
- **`scripts/uart-push-b64.py`** is the inverse: ship a host file to the board. Critical detail: the cmd line starts with `stty -echo` so the board's tty doesn't echo the 2+ MB base64 stream back (otherwise the kernel TX buffer interleaves echo bytes with the trailing `md5sum` / sentinel output and the host can't parse the result). The host write loop drains `s.in_waiting` after every chunk to keep the USB-serial RX ring buffer well below overflow.
- **`ebaz4205.cfg`** at the repo root is the OpenOCD top-level config (HS3 + zynq_7000 target, 5 MHz JTAG, srst_only).

## Things that bit us, don't redo

- `arm mcr 15 0 1 0 0 0x00C50078` looks like it should disable the MMU on a halted Cortex-A9 but didn't reliably take effect with Linux's MMU active. JTAG memory writes through the DAP went through the (still-active) Linux page tables and hit data aborts. The reliable disable is to *let the CPU itself* execute U-Boot's own MMU-disable in start.S — i.e., don't try to do warm-boot from running Linux; use SLCR-reset to get a fresh PS state instead.
- mainline U-Boot's `zynq-ebaz4205.dts` defaults `serial0 = &uart1` with `pinctrl_uart1_default` forcing MIO24/25. **Don't apply that pinctrl** — it overrides the miner FSBL's existing UART1 mux (which is on different MIOs that are physically wired to J7). Either drop `pinctrl-0 = <&pinctrl_uart1_default>` or skip mainline U-Boot entirely.
- The cpu1 spin-loop trick (parking cpu1 at `b .` while cpu0 boots U-Boot) requires the spin address to live **outside** the U-Boot binary's range (`0x04000000 .. 0x04101fc0`). Putting it at `0x04001000` corrupts itself when `load_image` overwrites that region.
- WSL inherits the Windows `PATH`, which contains entries with spaces (e.g. `/mnt/c/Program Files/...`). Buildroot rejects this with `Your PATH contains spaces, TABs, and/or newline (\n) characters`. Always strip Windows paths before invoking `make`.
- `dump_image` / `load_image` in OpenOCD use the CPU's current MMU context. With Linux running, you need the `phys` variants of `mdw`/`mww` for SLCR/peripheral access; for big block transfers, easier to halt → write SCTLR (verify by re-reading) → load.
- **Buildroot incremental rebuild does not pull newly enabled packages.** After flipping `BR2_PACKAGE_X=y` in defconfig, a plain `make` only re-runs the rootfs assembly stage and produces an identical-size `rootfs.jffs2` with the new binary missing. Force-build the package: `make <pkgname>` (e.g. `make xilinx-fpgautil`), then `make` again. Tell-tale symptom: `output/target/usr/bin/<binary>` is absent and rootfs.jffs2 mtime updated but size unchanged.
- **`uboot-intercept.py` leaves a residual `d` in U-Boot's input buffer** after detecting the prompt (it was hammering `'d'` to break autoboot). The very next command we send gets prefix-mangled — `loady 0x4000000` becomes `ddloady`, `Unknown command`. Always send a bare `\r` between intercept and the next U-Boot command. `nand-flash.py` runs are most affected.
- **Board's tty canonical-mode echo + interleaving traps host→board file pushes.** When a host script writes 2+ MB into a Linux shell on the board, the kernel echoes every byte back to host *and* interleaves those echo bytes with the actual `md5sum` / `echo` output the script produces afterwards — the trailing portion of the host's RX buffer becomes unparseable. Fix: prefix the cmd line with `stty -echo`, suffix with `stty echo`. With echo off, the host receives only the trailing md5 + sentinel + prompt cleanly. See `uart-push-b64.py`.
- **A failed `fpgautil -b <bad.bit>` leaves DEVCFG in a stuck state** (`Timeout waiting for PCFG_INIT` on subsequent loads). Retrying with a known-good bitstream from the same Linux session also fails. Only known recovery is power-cycle. The S2 reset button on this PCB rev is unreliable — physical Type-C unplug works. Don't deliberately load a corrupted bitstream unless you're prepared to power-cycle.
- **Reading a PL AXI register from Linux hangs the A9 *hard* unless FCLK0 is kept enabled.** The board dts shipped `&clkc { fclk-enable = <8>; }` (FCLK3 only — the GbE PHY ref clock). FCLK0 is then gated by `clk_disable_unused()` at late_initcall, so any AXI interconnect clocked by FCLK0 is frozen → a PS→PL read (`/dev/mem`, even a JTAG DAP mem-AP read) never completes, the CPU stalls so hard that **JTAG halt itself times out** and only a Type-C power-cycle recovers (SRST isn't wired; SLCR soft-reset needs a haltable core). Symptom under U-Boot is fine (U-Boot leaves FCLKs on) — the hang is Linux-only. Fix is `fclk-enable = <9>` (FCLK0|FCLK3), shipped as `buildroot/board/ebaz4205/patches/linux/0001-*.patch` (tracked; mirrored into `build/buildroot/` before building) via `BR2_GLOBAL_PATCH_DIR`. Safe way to probe PL-AXI without risking the hang: `openocd -f ebaz4205.cfg -c "target create zynq.ahb mem_ap -dap zynq.dap -ap-num 0" -c init -c "zynq.ahb mdw 0x41200000 1" -c shutdown` (returns a value if alive, WAIT-timeout error if dead, never wedges the CPU).

## Layout

- `ebaz4205.cfg` — OpenOCD top-level config for HS3 + Zynq-7000
- `scripts/` — all the bring-up plumbing (Python + bash + TCL)
- `build/buildroot/` — Buildroot 2026.05-git with `configs/ebaz4205_defconfig`. Output in `output/images/`
- `backup/` — preserved bits from the original NAND. Holds `boot.bin` (full mtd0 dump, 3 MB), `boot.bin.raw` (raw UART capture for post-mortem), `top.bit` (sliced PL bitstream from BOOT.BIN @ 0x19770, 2 MB; same content also persisted to mtd5)
- `bitstreams/`, `projects/` — intended for later FPGA work
- `img/`, `*.pdf`, `e4205.pcb` — board photos and Altium schematics for the main + V1.1 + V2.0 expansion + OV5640 + VGA-UART boards
- `.env/` — local Python 3.12 venv with `pyserial`, `pyftdi`, `pyusb`, `construct`. **It is a directory, not a dotenv file.**
