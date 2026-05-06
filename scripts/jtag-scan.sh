#!/usr/bin/env bash
# Quick JTAG smoke test: enumerate the EBAZ4205 chain.
# Expected output: zynq_pl.bs 0x13722093, zynq.cpu 0x4ba00477
set -euo pipefail
cd "$(dirname "$0")/.."
exec timeout 10 openocd -f ebaz4205.cfg -c "init; scan_chain; shutdown"
