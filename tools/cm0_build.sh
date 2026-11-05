#!/bin/sh
# Build the fixed-point sotto (8 kHz v3.7, or 16 kHz v3.8) for a Cortex-M0 (ARMv6-M: Baofeng
# UV-K6 / Quansheng UV-K5 class) with clang and lld, bare metal, for the cycle bench in
# c/fixed/cm0/run.py: c/fixed/cm0/build/bench.elf. Same as tools/cm4_build.sh, with
# c/fixed/cm0/rt.c for the division, 64-bit and CLZ helpers ARMv6-M lacks.
# LR=1 sh ... adds the sotto 1200 decoder (run.py --lr BITS).
# Usage: sh tools/cm0_build.sh [extra clang options, e.g. -Os]
#        CM0_OUT=dir sh tools/cm0_build.sh ...
# VARIANT=v3.6 / v3.8 as for the M4. Needs clang and ld.lld (tested with 18).
# SPDX-License-Identifier: MIT
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${CM0_OUT:-"$ROOT/c/fixed/cm0/build"}
CC=${CM0_CC:-clang}
LD=${CM0_LD:-ld.lld}
VFLAGS="-DSOTTO_V36"
[ "${VARIANT:-v3.7}" = v3.6 ] || VFLAGS="$VFLAGS -DSOTTO_PITCH_THIRD"
SRCS="fx_math fx_tables sotto_fx_enc sotto_fx_dec"
SYMS=""
# LR=1: also the sotto 1200 decoder (fixed/sotto_fx_lr.c), for run.py --lr
if [ -n "$LR" ]; then
  SRCS="$SRCS sotto_fx_lr"
  SYMS="$SYMS -u sotto_lr_dec_create -u sotto_lr_decode -u sotto_lr_dec_flush"
fi
if [ "${VARIANT:-v3.7}" = v3.8 ]; then
  SRCS="$SRCS sotto_fx_hiss"
  VFLAGS="$VFLAGS -DSOTTO_V38 -DFX_PCT_MAX=3000"
  SYMS="-u sotto_enc16_create -u sotto_encode16 -u sotto_enc16_flush -u sotto_dec16_create -u sotto_decode16 -u sotto_dec16_flush"
fi
RES=$("$CC" -print-resource-dir)
CFLAGS="--target=thumbv6m-none-eabi -mcpu=cortex-m0 -mthumb -mfloat-abi=soft -nostdinc
  -isystem $ROOT/c/fixed/cm4/inc -isystem $RES/include -ffunction-sections -fdata-sections -std=c99
  -Wall -Wno-unused-function $VFLAGS -I$ROOT/c -O2 $*"
mkdir -p "$OUT"
OBJS=""
for f in $SRCS; do
  $CC $CFLAGS -c "$ROOT/c/fixed/$f.c" -o "$OUT/$f.o"
  OBJS="$OBJS $OUT/$f.o"
done
$CC $CFLAGS -ffreestanding -fno-builtin -c "$ROOT/c/fixed/cm4/shim.c" -o "$OUT/shim.o"
$CC $CFLAGS -ffreestanding -fno-builtin -c "$ROOT/c/fixed/cm0/rt.c" -o "$OUT/rt.o"
$CC --target=thumbv6m-none-eabi -mcpu=cortex-m0 -c "$ROOT/c/fixed/cm0/divmod.s" -o "$OUT/divmod.o"
$LD -T "$ROOT/c/fixed/cm4/link.ld" --gc-sections -e sotto_encode -u sotto_enc_create -u sotto_enc_flush \
  -u sotto_dec_create -u sotto_decode -u sotto_dec_flush -u heap_used $SYMS $OBJS "$OUT/shim.o" "$OUT/rt.o" "$OUT/divmod.o" -o "$OUT/bench.elf"
echo "built $OUT/bench.elf"
