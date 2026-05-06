#!/usr/bin/env bash
# Halt Cortex-A9 core 0 and dump core registers via OpenOCD.
# Useful to verify PS-side debug works (DDR/clock init must already be running).
set -euo pipefail
cd "$(dirname "$0")/.."
exec openocd -f ebaz4205.cfg -c "
init
targets zynq.cpu0
halt
reg
resume
shutdown
"
