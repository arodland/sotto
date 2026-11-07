#!/bin/sh
# Transmit the K6 reset control packet (action 7, F/1800) through the IC-7100: the --trigger for
#   python3 tools/k6_update.py IMAGE --trigger "sh ~/sotto-work/bin/k6-reset-trigger.sh" --window 0.6
# k6_bench.py send takes the PTT lock, checks the IC-7100 is on 70 cm and on the K6's frequency
# (the last CFG in $K6LOG when that is set, else 440.850), and sends the CW ID first when one is
# due. `python3 tools/k6_bench.py install` copies this script and k6_bench.py to $SOTTO_WORK/bin
# (default ~/sotto-work/bin) and makes $SOTTO_WORK/state/reset.wav. Opens no serial port.
set -eu
W=${SOTTO_WORK:-$HOME/sotto-work}
B=$(dirname "$0")
WAV=$W/state/reset.wav
[ -f "$WAV" ] || python3 "$B/k6_bench.py" wavs
if [ -n "${K6LOG:-}" ]; then
  exec python3 "$B/k6_bench.py" send --k6log "$K6LOG" "$WAV"
fi
exec python3 "$B/k6_bench.py" send --freq 440850000 "$WAV"
