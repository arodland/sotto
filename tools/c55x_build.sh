#!/bin/sh
# Build the fixed-point sotto for the TI C5517 with TI's C5500 compiler (cl55, CGT 4.4.1):
# c/fixed/c55x/build/bench.out, plus the assembly listings (.asm) and the link map.
# Usage: C55X_CGT=/path/to/c5500_4.4.1 sh tools/c55x_build.sh [extra cl55 options]
#        C55X_MAIN=fxtest sh tools/c55x_build.sh   builds fxtest.out (primitive checksums)
#        sh tools/c55x_build.sh -DBENCH16           bench.out for 16 kHz in.raw through the v3.8 layer
# The build is v3.8 (SOTTO_V38: the fixed/sotto_fx_hiss.c layer); C55X_VARIANT=v3.7 leaves it out.
# Run make -C c fixed first, for c/fixed/fx_tables.c.
# SPDX-License-Identifier: MIT
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CGT=${C55X_CGT:-/opt/ti/c5500_4.4.1/c5500_4.4.1}
MAIN=${C55X_MAIN:-bench}
OUT=${C55X_OUT:-"$ROOT/c/fixed/c55x/build"}
mkdir -p "$OUT"
cd "$OUT"
# -vcpu:3.3: the C5517's CPU revision; large memory model; --gcc for aggregate initialisers
# that aren't constant; -k keeps the assembly
VARIANT=${C55X_VARIANT:-v3.8}
SRCS="fx_math fx_tables sotto_fx_enc sotto_fx_dec"
VFLAGS="-DSOTTO_PITCH_THIRD"
if [ "$VARIANT" = v3.8 ]; then SRCS="$SRCS sotto_fx_hiss"; VFLAGS="$VFLAGS -DSOTTO_V38"; fi
OPTS="-vcpu:3.3 --memory_model=large -O3 --gcc -k $VFLAGS -I$CGT/include -I$ROOT/c $*"
OBJS=""
for f in $SRCS; do
  "$CGT/bin/cl55" $OPTS --diag_suppress=1173 -c "$ROOT/c/fixed/$f.c"
  OBJS="$OBJS $f.obj"
done
"$CGT/bin/cl55" $OPTS -c "$ROOT/c/fixed/c55x/$MAIN.c"
# the 16 kHz layer's states need a larger heap, outside DARAM (bench16.cmd)
CMD=bench.cmd HEAP=0x8000                  # heap size in bytes
case " $* " in *-DBENCH16*) CMD=bench16.cmd HEAP=0x1F000;; esac   # bytes
"$CGT/bin/cl55" $OPTS -z --rom_model --stack_size=0x2000 --sys_stacksize=0x1000 --heap_size=$HEAP -o $MAIN.out -m $MAIN.map -i"$CGT/lib" \
  $MAIN.obj $OBJS \
  "$ROOT/c/fixed/c55x/$CMD" -lrts55x.lib
echo "built $OUT/$MAIN.out"
