#!/bin/sh
# Build the K6 voice pager's receive library for the Baofeng UV-K6 (KD32F328, Cortex-M0):
# c/k6/build/libsotto_k6.a, the v3.7 8 kHz fixed-point decoder (FX_NARROW_MUL, fx_oa_block;
# FX_CS512_CONST keeps its 2 KB cos|sin table in flash, ~1 MHz more, for the radio's stack)
# plus the packet reassembly and playback in c/k6/sotto_pager.c, for Aura (Rust) to link.
# Also c/k6/build/bench.elf, the same code linked alone for c/k6/bench_k6.py's cycle count.
#
# Usage: sh tools/k6_build.sh [extra clang options]
#        K6_CODEC=1200 sh tools/k6_build.sh   the sotto 1200 build (-DSP_LR, plays magic 0xA9
#            messages, 40 ms per slot; c/fixed/sotto_fx_lr.c at -Os) into c/k6/build-lr
#        K6_MOD_KEY_FILE=~/.config/sotto/k6-mod.key sh tools/k6_build.sh   the RAM module loader
#            (c/k6/k6_mod.c) accepts modules signed with that key (`python -m modem.k6 modkey`
#            makes one). Without it the loader holds a placeholder and refuses every module
#            (CI's build) until `python -m modem.k6 keyimage` writes a key into the .bin. A keyed library or image holds the key: keep it on your machine.
#        K6_CB=spi sh tools/k6_build.sh   sotto's 30 KB shape codebook lives in the radio's SPI flash
#            (c/k6/sp_cb.h, at 0x100000) instead of the MCU's: the decoder reads each 20-byte
#            vector when it needs it (-DFX_CB_SPI) and the linker drops the table. The image to
#            put there is $OUT/sotto-cb.bin, and its CRC (-DSP_CB_CRC) is what the radio checks.
#        K6_CB=install   the same, with the table linked: at power-on the radio writes the image
#            to the SPI flash if it isn't there yet (once), then reads from it (with K6_CODEC=1200
#            it doesn't fit beside Aura). Each writes c/k6/build[-lr]-cbs or -cbi unless K6_OUT is set.
#        K6_RAMFUNC=1 sh tools/k6_build.sh   puts fx_oa_block (0.8 KB, the decoder's hottest
#            loop) in a .data section, which cortex-m-rt copies to RAM at boot: ~5 MHz less,
#            0.8 KB more RAM. Off by default for the first bring-up.
# The library leaves memcpy, memset and the ARM run-time helpers (__aeabi_uidiv, __aeabi_lmul,
# __aeabi_llsl, ...) undefined: Rust's compiler_builtins provides them for thumbv6m-none-eabi.
# Needs clang and ld.lld (tested with 18). SPDX-License-Identifier: MIT
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [ "${K6_CODEC:-}" = 1200 ]; then
  OUT=${K6_OUT:-"$ROOT/c/k6/build-lr"}
  LR=-DSP_LR
else
  OUT=${K6_OUT:-"$ROOT/c/k6/build"}
  LR=
fi
CB=
case "${K6_CB:-}" in
  "") ;;
  spi) OUT=${K6_OUT:-$OUT-cbs} ;;
  install) OUT=${K6_OUT:-$OUT-cbi}; CB="-DFX_CB_SPI -DSP_CB_INSTALL" ;;
  *) echo "K6_CB is spi or install" >&2; exit 1 ;;
esac
CC=${K6_CC:-clang}
AR=${K6_AR:-llvm-ar}
OBJCOPY=${K6_OBJCOPY:-llvm-objcopy}
RES=$("$CC" -print-resource-dir)
CFLAGS="--target=thumbv6m-none-eabi -mcpu=cortex-m0 -mthumb -mfloat-abi=soft -nostdinc
  -isystem $ROOT/c/fixed/cm4/inc -isystem $RES/include -ffunction-sections -fdata-sections -std=c99
  -ffreestanding -fno-builtin-calloc -fno-builtin-free -Wall -Wno-unused-function
  -DSOTTO_V36 -DSOTTO_PITCH_THIRD -DFX_CS512_CONST -DFX_YIELD_HOOK $LR -I$ROOT/c -O2 $*"
mkdir -p "$OUT"
if [ -n "${K6_CB:-}" ]; then
  # the codebook image and its CRC, from this tree's fx_tables.c (a host build of c/k6/sp_cb.c)
  ${HOSTCC:-cc} -O2 -std=c99 -I"$ROOT/c" "$ROOT/c/k6/cb_image.c" "$ROOT/c/k6/sp_cb.c" "$ROOT/c/fixed/fx_tables.c" \
    -o "$OUT/cb_image"
  "$OUT/cb_image" "$OUT/sotto-cb.bin"
  [ "$K6_CB" = spi ] && CB="-DFX_CB_SPI -DSP_CB_CRC=$("$OUT/cb_image" -c)"
  CFLAGS="$CFLAGS $CB"
  echo "codebook in SPI flash: $CB"
fi
KM=
if [ -n "${K6_MOD_KEY_FILE:-}" ]; then
  KM=$(python3 -c 'import sys; k = bytes.fromhex(open(sys.argv[1]).read().strip()); assert len(k) == 16, "want 32 hex digits"; print(",".join(str(b) for b in k))' "$K6_MOD_KEY_FILE")
  echo "module loader: key from $K6_MOD_KEY_FILE"
else
  echo "module loader: no K6_MOD_KEY_FILE, so it refuses every module"
fi
$CC $CFLAGS -Os ${KM:+-DKM_KEY=$KM} -c "$ROOT/c/k6/k6_mod.c" -o "$OUT/k6_mod.o"
# per packet, not per sample (the PWM conversion is 160 samples in 20 ms): -Os, about 2 KB less
$CC $CFLAGS ${K6_PAGER_OPT:--Os} -c "$ROOT/c/k6/sotto_pager.c" -o "$OUT/sotto_pager.o"
$CC $CFLAGS -Dcalloc=sp_calloc -Dfree=sp_free -c "$ROOT/c/fixed/sotto_fx_dec.c" -o "$OUT/sotto_fx_dec.o"
$CC $CFLAGS -c "$ROOT/c/fixed/fx_math.c" -o "$OUT/fx_math.o"
$CC $CFLAGS -c "$ROOT/c/fixed/fx_tables.c" -o "$OUT/fx_tables.o"
$CC $CFLAGS -Os -c "$ROOT/c/k6/sp_cb.c" -o "$OUT/sp_cb.o"
if [ "${K6_RAMFUNC:-0}" = 1 ]; then
  $OBJCOPY --rename-section .text.fx_oa_block=.data.ramfunc.fx_oa_block,alloc,load,contents,code "$OUT/fx_math.o"
fi
LIBOBJS="$OUT/sotto_pager.o $OUT/k6_mod.o $OUT/sotto_fx_dec.o $OUT/sp_cb.o $OUT/fx_math.o $OUT/fx_tables.o"
if [ -n "$LR" ]; then     # not hot (field unpacking and shape sums once per 40 ms): -Os
  $CC $CFLAGS -Os -Dcalloc=sp_calloc -Dfree=sp_free -c "$ROOT/c/fixed/sotto_fx_lr.c" -o "$OUT/sotto_fx_lr.o"
  LIBOBJS="$LIBOBJS $OUT/sotto_fx_lr.o"
fi
rm -f "$OUT/libsotto_k6.a"
$AR rcs "$OUT/libsotto_k6.a" $LIBOBJS
echo "built $OUT/libsotto_k6.a"
# the bench: the library alone, with the cm0 bench's run-time helpers and memory functions
# (and with the codebook in SPI flash, c/k6/flash_bench.c's stand-in flash, which serves the
# image from the MCU's flash: the decode is the same, the SPI transfers aren't counted)
BENCHCB=
if [ -n "${K6_CB:-}" ]; then
  python3 -c 'import sys; d = open(sys.argv[1], "rb").read(); print("const unsigned char sp_cb_bench_image[%d] = {%s};" % (len(d), ",".join(map(str, d))))' \
    "$OUT/sotto-cb.bin" > "$OUT/cb_bench_image.c"
  $CC $CFLAGS -c "$OUT/cb_bench_image.c" -o "$OUT/cb_bench_image.o"
  $CC $CFLAGS -fno-builtin -c "$ROOT/c/k6/flash_bench.c" -o "$OUT/flash_bench.o"
  BENCHCB="$OUT/cb_bench_image.o $OUT/flash_bench.o"
fi
$CC $CFLAGS -fno-builtin -c "$ROOT/c/fixed/cm4/shim.c" -o "$OUT/shim.o"
$CC $CFLAGS -fno-builtin -c "$ROOT/c/fixed/cm0/rt.c" -o "$OUT/rt.o"
$CC --target=thumbv6m-none-eabi -mcpu=cortex-m0 -c "$ROOT/c/fixed/cm0/divmod.s" -o "$OUT/divmod.o"
ld.lld -T "$ROOT/c/fixed/cm4/link.ld" --gc-sections -e sp_init -u sp_packet -u sp_status -u sp_play_start \
  -u sp_play_frame -u sp_play_pwm -u sp_pcm_to_pwm -u sp_test_packet -u sp_last_packet -u sp_static_bytes -u sotto_fx_dec_bytes -u heap_used -u sp_set_yield -u sp_yield_probe -u sp_cb_setup -u sp_cb_bench \
  $LIBOBJS $BENCHCB "$OUT/shim.o" "$OUT/rt.o" "$OUT/divmod.o" -o "$OUT/bench.elf"
echo "built $OUT/bench.elf"
