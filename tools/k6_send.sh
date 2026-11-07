#!/bin/sh
# Key the IC-7100 and play one WAV, on 70 cm only. The bench's send() helper as a script,
# so one Claude Code allow rule can cover it (docs/hamlet-permissions.md).
#
#   sh tools/k6_send.sh ctl.wav
#
# tools/k6_bench.py send does the work: it takes the bench's PTT lock (shared with the CW ID
# keeper and every keying tool), refuses unless the IC-7100 (rigctld, rigctl -m 2) reads
# 430.010-449.990 MHz (the 70 cm band north of Line A) and is not keyed, refuses a WAV over
# 180 s, sends the CW ID first when the last one is over 8.5 min old, and always unkeys.
# Opens no serial port: PTT goes through rigctld on /dev/ttyUSB0, never /dev/ttyUSB2.
set -eu
[ $# -eq 1 ] || { echo "usage: sh tools/k6_send.sh FILE.wav" >&2; exit 2; }
exec python3 "$(dirname "$0")/k6_bench.py" send --max-s 180 "$1"
