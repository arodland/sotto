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
#        K6_CODEC=vlr sh tools/k6_build.sh   sotto 1200 plus sotto 600 / 400 (-DSP_LR -DSP_VLR: magic
#            0xA9, 0xAC, 0xAD; c/fixed/sotto_fx_vlr.c and c/sotto_vlr_tables.c) into c/k6/build-vlr.
#            With K6_CB=spi both codebooks are read from the SPI flash: v3.7's at 0x100000 and
#            sotto 600 / 400's 21 KB at 0x108000 (c/k6/sp_vlr.h, image $OUT/sotto-vlr.bin, CRC
#            -DSP_VLR_CRC); with K6_CB=install the sotto 600 / 400 tables are linked and written
#            there once at power-on (v3.7's codebook must be there already: aura-pager-cbi);
#            with K6_CB=rom they are linked and read in place from the MCU's flash, as ROM, and
#            the image never writes the SPI flash (it reads v3.7's codebook there, for sotto 1200).
#        K6_CODEC=vlr K6_HISS=1 sh tools/k6_build.sh   the hiss band build (docs/k6-hiss-port.md): sotto 1200,
#            600 and 400 played at 16 kHz through the K6 hiss layer (c/fixed/sotto_fx_hiss.c with
#            -DFX_HISS_LIGHT -DFX_HISS_FB on the v3.8 core; -DSP_HISS: 320-sample frames) into
#            c/k6/build-vlrh[-cbs]; the firmware's `hiss` feature matches its PWM ring to it
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
VLR=
HISS=
if [ "${K6_CODEC:-}" = 1200 ]; then
  OUT=${K6_OUT:-"$ROOT/c/k6/build-lr"}
  LR=-DSP_LR
elif [ "${K6_CODEC:-}" = vlr ]; then
  OUT=${K6_OUT:-"$ROOT/c/k6/build-vlr"}
  LR="-DSP_LR -DSP_VLR"
  VLR=1
  if [ "${K6_HISS:-0}" = 1 ]; then
    OUT=${K6_OUT:-"$ROOT/c/k6/build-vlrh"}
    LR="$LR -DSP_HISS -DSOTTO_V38 -DFX_HISS_LIGHT -DFX_HISS_FB -DFX_HISS_NO_ENC"
    HISS=1
  fi
else
  OUT=${K6_OUT:-"$ROOT/c/k6/build"}
  LR=
fi
# K6_VOICE=0: the EXPLORE image's library (firmware/k6-aura/build.sh aura-explore), the voice
# decoder left out (c/k6/sp_novoice.c stands in for it), into c/k6/build-explore
if [ "${K6_VOICE:-1}" = 0 ]; then
  [ -z "$LR" ] || { echo "K6_VOICE=0 is the v3.7 build without its decoder: no K6_CODEC" >&2; exit 1; }
  OUT=${K6_OUT:-"$ROOT/c/k6/build-explore"}
  K6_OUT=$OUT
fi
CB=
case "${K6_CB:-}" in
  "") ;;
  spi) OUT=${K6_OUT:-$OUT-cbs} ;;
  install) OUT=${K6_OUT:-$OUT-cbi}; [ -n "$VLR" ] || CB="-DFX_CB_SPI -DSP_CB_INSTALL" ;;
  rom) [ -n "$VLR" ] || { echo "K6_CB=rom is for K6_CODEC=vlr" >&2; exit 1; }; OUT=${K6_OUT:-$OUT-rom} ;;
  *) echo "K6_CB is spi, install or rom" >&2; exit 1 ;;
esac
# the images that link the 30 KB shape codebook into the MCU's flash (the -table fallbacks and the
# install image) have no room for the parity rebuild (spec 14): they ignore parity packets
NOPAR=
case "${K6_CB:-}:$VLR" in
  :*|install:) NOPAR=-DSP_NO_PARITY ;;
esac
# the images with the coded 4FSK session (c/k6/ifpager.c: not lean) are the ones with the codebook
# in SPI flash: there sp_packet yields to the session's receiver while a packet goes in
PKY=
[ "${K6_CB:-}" = spi ] && PKY=-DSP_PKT_YIELD
CC=${K6_CC:-clang}
AR=${K6_AR:-llvm-ar}
OBJCOPY=${K6_OBJCOPY:-llvm-objcopy}
RES=$("$CC" -print-resource-dir)
CFLAGS="--target=thumbv6m-none-eabi -mcpu=cortex-m0 -mthumb -mfloat-abi=soft -nostdinc
  -isystem $ROOT/c/fixed/cm4/inc -isystem $RES/include -ffunction-sections -fdata-sections -std=c99
  -ffreestanding -fno-builtin-calloc -fno-builtin-free -Wall -Wno-unused-function
  -DSOTTO_V36 -DSOTTO_PITCH_THIRD -DFX_CS512_CONST -DFX_YIELD_HOOK $LR -I$ROOT/c -O2 $NOPAR $PKY $*"
mkdir -p "$OUT"
if [ -n "${K6_CB:-}" ]; then
  # the codebook image and its CRC, from this tree's fx_tables.c (a host build of c/k6/sp_cb.c)
  ${HOSTCC:-cc} -O2 -std=c99 -I"$ROOT/c" "$ROOT/c/k6/cb_image.c" "$ROOT/c/k6/sp_cb.c" "$ROOT/c/fixed/fx_tables.c" \
    -o "$OUT/cb_image"
  "$OUT/cb_image" "$OUT/sotto-cb.bin"
  if [ "$K6_CB" = spi ] || [ -n "$VLR" ]; then CB="-DFX_CB_SPI -DSP_CB_CRC=$("$OUT/cb_image" -c)"; fi
  if [ -n "$VLR" ]; then
    # sotto 600 / 400's codebooks: their image and CRC (c/k6/vlr_image, a host build)
    ${HOSTCC:-cc} -O2 -std=c99 -DSP_VLR -I"$ROOT/c" "$ROOT/c/k6/vlr_image.c" "$ROOT/c/k6/sp_vlr.c" "$ROOT/c/k6/sp_cb.c" \
      "$ROOT/c/sotto_vlr_tables.c" "$ROOT/c/fixed/fx_tables.c" -o "$OUT/vlr_image"
    "$OUT/vlr_image" "$OUT/sotto-vlr.bin"
    # rom: no VLR_CB_SPI, so the tables are linked and sp_vlr_setup touches no flash
    case "$K6_CB" in
      spi) CB="$CB -DVLR_CB_SPI -DSP_VLR_CRC=$("$OUT/vlr_image" -c)" ;;
      install) CB="$CB -DVLR_CB_SPI -DSP_VLR_INSTALL" ;;
    esac
  fi
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
$CC $CFLAGS -Os -c "$ROOT/c/k6/sp_vlr.c" -o "$OUT/sp_vlr.o"
if [ "${K6_RAMFUNC:-0}" = 1 ]; then
  $OBJCOPY --rename-section .text.fx_oa_block=.data.ramfunc.fx_oa_block,alloc,load,contents,code "$OUT/fx_math.o"
fi
# the coded 4FSK receiver (PHY G: ifrx's IF matched filter on the 600-2400 Hz half-baud tones,
# soft Viterbi): linked only by the images that call it (not the lean ones). -Os, as the
# emulator's timing of it (tests/k6_live_emu.py run_ifpager)
$CC $CFLAGS -Os -c "$ROOT/c/k6/ifpager.c" -o "$OUT/ifpager.o"
# the EXPLORE app's engine (docs/k6-explore-app.md): the sweep a block at a time, the candidate
# list, the repeater table and Listen for ID. -Oz: none of it is hot next to a 1.5 ms read, and
# only the images that call it link it (rt.c goes in as k6_rt.o: c/fixed/cm0/rt.c is the bench's)
for f in xscan xlisten explore cwid; do $CC $CFLAGS -Oz -c "$ROOT/c/k6/$f.c" -o "$OUT/$f.o"; done
$CC $CFLAGS -Oz -c "$ROOT/c/k6/rt.c" -o "$OUT/k6_rt.o"
# Listen for ID's side detectors (DMR syncs on the GP0 IF, the CTCSS read): -Os, the DMR
# detector runs on every count (as the emulator's timing of it, tests/test_k6_xdmr.py)
$CC $CFLAGS -Os -c "$ROOT/c/k6/xdmr.c" -o "$OUT/xdmr.o"
LIBOBJS="$OUT/sotto_pager.o $OUT/k6_mod.o $OUT/sotto_fx_dec.o $OUT/sp_cb.o $OUT/sp_vlr.o $OUT/fx_math.o $OUT/fx_tables.o $OUT/ifpager.o
  $OUT/xscan.o $OUT/xlisten.o $OUT/xdmr.o $OUT/explore.o $OUT/cwid.o $OUT/k6_rt.o"
if [ -n "$LR" ]; then     # not hot (field unpacking and shape sums once per 40 ms): -Os
  $CC $CFLAGS -Os -Dcalloc=sp_calloc -Dfree=sp_free -c "$ROOT/c/fixed/sotto_fx_lr.c" -o "$OUT/sotto_fx_lr.o"
  LIBOBJS="$LIBOBJS $OUT/sotto_fx_lr.o"
fi
if [ -n "$VLR" ]; then    # once per 20 ms frame, then the v3.7 core's synthesis: -Os too
  $CC $CFLAGS -Os -Dcalloc=sp_calloc -Dfree=sp_free -c "$ROOT/c/fixed/sotto_fx_vlr.c" -o "$OUT/sotto_fx_vlr.o"
  # the tables build: with K6_CB=spi the codebooks are left out (VLR_CB_SPI); install and rom link them
  VT=; [ "${K6_CB:-}" = install ] && VT=-UVLR_CB_SPI
  $CC $CFLAGS $VT -c "$ROOT/c/sotto_vlr_tables.c" -o "$OUT/sotto_vlr_tables.o"
  LIBOBJS="$LIBOBJS $OUT/sotto_fx_vlr.o $OUT/sotto_vlr_tables.o"
fi
UHISS=
if [ -n "$HISS" ]; then   # the layer at -O2 without loop unrolling: 6.8 KB of flash (11 KB unrolled, which
                          # doesn't fit beside the rest), -Os's size at -O2's speed on the M0
  $CC $CFLAGS -fno-unroll-loops -Dcalloc=sp_calloc -Dfree=sp_free -c "$ROOT/c/fixed/sotto_fx_hiss.c" -o "$OUT/sotto_fx_hiss.o"
  LIBOBJS="$LIBOBJS $OUT/sotto_fx_hiss.o"
  UHISS="-u sotto_fx_lr_dec16_bytes -u sotto_fx_vlr_dec16_bytes"
fi
if [ "${K6_VOICE:-1}" = 0 ]; then
  $CC $CFLAGS -Os -c "$ROOT/c/k6/sp_novoice.c" -o "$OUT/sp_novoice.o"
  LIBOBJS=$(echo $LIBOBJS | sed "s# [^ ]*/sotto_fx_dec.o# $OUT/sp_novoice.o#; s# [^ ]*/fx_math.o##")
fi
rm -f "$OUT/libsotto_k6.a"
$AR rcs "$OUT/libsotto_k6.a" $LIBOBJS
echo "built $OUT/libsotto_k6.a"
# no bench for the EXPLORE image's library: it has no decoder to time
[ "${K6_VOICE:-1}" = 0 ] && exit 0
# the bench: the library alone, with the cm0 bench's run-time helpers and memory functions
# (and with the codebook in SPI flash, c/k6/flash_bench.c's stand-in flash, which serves the
# image from the MCU's flash: the decode is the same, the SPI transfers aren't counted)
BENCHCB=
if [ -n "${K6_CB:-}" ]; then
  python3 -c 'import sys; d = open(sys.argv[1], "rb").read(); print("const unsigned char sp_cb_bench_image[%d] = {%s};" % (len(d), ",".join(map(str, d))))' \
    "$OUT/sotto-cb.bin" > "$OUT/cb_bench_image.c"
  if [ -n "$VLR" ]; then
    python3 -c 'import sys; d = open(sys.argv[1], "rb").read(); print("const unsigned char sp_vlr_bench_image[%d] = {%s};" % (len(d), ",".join(map(str, d))))' \
      "$OUT/sotto-vlr.bin" >> "$OUT/cb_bench_image.c"
  fi
  $CC $CFLAGS -c "$OUT/cb_bench_image.c" -o "$OUT/cb_bench_image.o"
  $CC $CFLAGS -fno-builtin -c "$ROOT/c/k6/flash_bench.c" -o "$OUT/flash_bench.o"
  BENCHCB="$OUT/cb_bench_image.o $OUT/flash_bench.o"
fi
$CC $CFLAGS -fno-builtin -c "$ROOT/c/fixed/cm4/shim.c" -o "$OUT/shim.o"
$CC $CFLAGS -fno-builtin -c "$ROOT/c/fixed/cm0/rt.c" -o "$OUT/rt.o"
$CC --target=thumbv6m-none-eabi -mcpu=cortex-m0 -c "$ROOT/c/fixed/cm0/divmod.s" -o "$OUT/divmod.o"
ld.lld -T "$ROOT/c/fixed/cm4/link.ld" --gc-sections -e sp_init -u sp_packet -u sp_status -u sp_play_start \
  -u sp_play_frame -u sp_play_pwm -u sp_pcm_to_pwm -u sp_test_packet -u sp_last_packet -u sp_static_bytes -u sotto_fx_dec_bytes -u heap_used -u sp_set_yield -u sp_yield_probe -u sp_set_packet_yield -u sp_set_stream -u sp_cb_setup -u sp_cb_bench -u sp_vlr_setup $UHISS \
  $LIBOBJS $BENCHCB "$OUT/shim.o" "$OUT/rt.o" "$OUT/divmod.o" -o "$OUT/bench.elf"
echo "built $OUT/bench.elf"
