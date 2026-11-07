#!/bin/sh
# Key the IC-7100 and play one WAV, on 70 cm only. The bench's send() helper as a script,
# so one Claude Code allow rule can cover it (docs/hamlet-permissions.md).
#
#   sh tools/k6_send.sh ctl.wav
#
# Refuses unless the IC-7100 (rigctld, rigctl -m 2) reads 430.010-449.990 MHz (the 70 cm
# band north of Line A, as tools/k6_rxmap.py checks) and is not already keyed (the CW ID
# keeper shares the rig). Plays at most K6_SEND_MAX_S seconds (180). Always unkeys on exit.
# Opens no serial port: PTT goes through rigctld on /dev/ttyUSB0, never /dev/ttyUSB2.
set -eu

SINK=alsa_output.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo
MAX_S=180
LO=430010000
HI=449990000

[ $# -eq 1 ] || { echo "usage: sh tools/k6_send.sh FILE.wav" >&2; exit 2; }
WAV=$1
[ -f "$WAV" ] || { echo "no such file: $WAV" >&2; exit 2; }

f=$(rigctl -m 2 f | head -n1)
case $f in ''|*[!0-9]*) echo "refusing: rigctl f gave '$f'" >&2; exit 1;; esac
if [ "$f" -lt $LO ] || [ "$f" -gt $HI ]; then
  echo "refusing: IC-7100 on $f Hz, outside 430.010-449.990 MHz" >&2; exit 1
fi
t=$(rigctl -m 2 t | head -n1)
[ "$t" = 0 ] || { echo "already keyed (t=$t): wait for the ID keeper" >&2; exit 1; }

unkey() { trap - EXIT INT TERM HUP; rigctl -m 2 T 0 >/dev/null 2>&1 || true; }
trap unkey EXIT INT TERM HUP
rigctl -m 2 T 1
sleep 0.2
timeout "$MAX_S" paplay --device="$SINK" "$WAV" || echo "paplay ended with $? (124: hit ${MAX_S} s)" >&2
sleep 0.05
unkey
echo "sent $WAV on $f Hz, PTT now $(rigctl -m 2 t | head -n1)"
