#!/bin/sh
# Count how often the inner loops of the fixed-point encoder and decoder run, per second of
# speech, as the basis of the C55x cycle estimate in docs/c-port.md. Builds the c/fixed
# sources with gcov coverage in a temporary folder and codes 10 s of ve9qrp.
# Usage: sh tools/fx_profile.sh   (run make -C c fixed first, for fx_tables.c)
# SPDX-License-Identifier: MIT
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
cp "$ROOT"/c/sotto.h "$ROOT"/c/sotto_lr_tables.h "$ROOT"/c/sotto_lr_dec.inc "$ROOT"/c/sotto_enc.c "$ROOT"/c/sotto_dec.c "$ROOT"/c/fixed/*.c "$ROOT"/c/fixed/*.h "$T"/
cd "$T"
sed -i 's#"../sotto\(_lr_tables\)*.h"#"sotto\1.h"#' sotto_fx_enc.c sotto_fx_dec.c sotto_fx_lr.c sotto_fx_lr_enc.c fx_internal.h
sed -i 's#"../sotto_lr_dec.inc"#"sotto_lr_dec.inc"#' sotto_fx_lr.c
cc -O0 --coverage -o enc sotto_enc.c sotto_fx_enc.c sotto_fx_lr_enc.c fx_math.c fx_tables.c
cc -O0 --coverage -o dec sotto_dec.c sotto_fx_dec.c sotto_fx_lr.c fx_math.c fx_tables.c
RAW="$ROOT/codec/codec2/raw/ve9qrp_10s.raw"
SECONDS_IN=$(( $(wc -c < "$RAW") / 16000 ))
./enc "$RAW" out.bit
./dec out.bit out.raw
gcov -o enc-sotto_fx_enc sotto_fx_enc.c > /dev/null
gcov -o enc-fx_math fx_math.c > /dev/null && mv fx_math.c.gcov enc_fx_math.c.gcov
gcov -o dec-sotto_fx_dec sotto_fx_dec.c > /dev/null
# the busiest lines, as executions per second of speech
for f in sotto_fx_enc.c.gcov enc_fx_math.c.gcov sotto_fx_dec.c.gcov; do
  echo "== $f (executions per second, line, source)"
  awk -F: -v s="$SECONDS_IN" '$1+0 > 0 { printf "%10d  %5d %s\n", $1 / s, $2, $3 }' "$f" | sort -rn | head -12
done
