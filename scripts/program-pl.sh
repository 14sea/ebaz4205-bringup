#!/usr/bin/env bash
# Program a bitstream onto the PL of the EBAZ4205 over JTAG.
# Usage: program-pl.sh path/to/design.bit
set -euo pipefail
BIT="${1:?usage: program-pl.sh <bitstream.bit>}"
[[ -r "$BIT" ]] || { echo "cannot read $BIT" >&2; exit 1; }
cd "$(dirname "$0")/.."
exec openocd -f ebaz4205.cfg -c "init; pld load 0 $(realpath "$BIT"); shutdown"
