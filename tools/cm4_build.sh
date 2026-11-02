#!/bin/sh
# Build the fixed-point sotto (8 kHz v3.7, or 16 kHz v3.8) for a Cortex-M4 with clang and lld, bare metal, for
# the cycle bench in c/fixed/cm4/run.py: c/fixed/cm4/build/bench.elf.
# Usage: sh tools/cm4_build.sh [extra clang options, e.g. -Os or -DFX_SPLIT_MUL]
#        CM4_OUT=dir sh tools/cm4_build.sh ...
# Default -O2 (OpenRTX builds with -Os; pass -Os to see that), unaligned access on (as GCC
# does for the Cortex-M4). VARIANT=v3.6 drops the pitch third; VARIANT=v3.8 adds the 16 kHz
# hiss band (fixed/sotto_fx_hiss.c, 30 s of percentile history as on the C55x bench; run.py
# --16k times it). Needs clang and ld.lld (any recent LLVM; tested with 18) and nothing from ARM.
# SPDX-License-Identifier: MIT
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${CM4_OUT:-"$ROOT/c/fixed/cm4/build"}
CC=${CM4_CC:-clang}
LD=${CM4_LD:-ld.lld}
VFLAGS="-DSOTTO_V36"
[ "${VARIANT:-v3.7}" = v3.6 ] || VFLAGS="$VFLAGS -DSOTTO_PITCH_THIRD"
SRCS="fx_math fx_tables sotto_fx_enc sotto_fx_dec"
SYMS=""
if [ "${VARIANT:-v3.7}" = v3.8 ]; then
  SRCS="$SRCS sotto_fx_hiss"
  VFLAGS="$VFLAGS -DSOTTO_V38 -DFX_PCT_MAX=3000"
  SYMS="-u sotto_enc16_create -u sotto_encode16 -u sotto_enc16_flush -u sotto_dec16_create -u sotto_decode16 -u sotto_dec16_flush"
fi
RES=$("$CC" -print-resource-dir)
CFLAGS="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mthumb -mfloat-abi=soft -munaligned-access -nostdinc
  -isystem $ROOT/c/fixed/cm4/inc -isystem $RES/include -ffunction-sections -fdata-sections -std=c99
  -Wall -Wno-unused-function $VFLAGS -I$ROOT/c -O2 $*"
mkdir -p "$OUT"
OBJS=""
for f in $SRCS; do
  $CC $CFLAGS -c "$ROOT/c/fixed/$f.c" -o "$OUT/$f.o"
  OBJS="$OBJS $OUT/$f.o"
done
$CC $CFLAGS -ffreestanding -fno-builtin -c "$ROOT/c/fixed/cm4/shim.c" -o "$OUT/shim.o"
$CC --target=thumbv7em-none-eabi -mcpu=cortex-m4 -c "$ROOT/c/fixed/cm4/divmod.s" -o "$OUT/divmod.o"
$LD -T "$ROOT/c/fixed/cm4/link.ld" --gc-sections -e sotto_encode -u sotto_enc_create -u sotto_enc_flush \
  -u sotto_dec_create -u sotto_decode -u sotto_dec_flush -u heap_used $SYMS $OBJS "$OUT/shim.o" "$OUT/divmod.o" -o "$OUT/bench.elf"
echo "built $OUT/bench.elf"
