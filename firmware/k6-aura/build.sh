#!/bin/sh
# Build Aura (the open Rust firmware for the Baofeng UV-K6 / K61 family) with the sotto voice
# pager patched in: images in firmware/k6-aura/out/, ready for Aura's tools/flash.py.
#
#   sh firmware/k6-aura/build.sh      ->  out/aura-pager-lr-cbs.bin, the image to flash (and size reports)
#
# The default image is aura-pager-lr-cbs: sotto 1200, with sotto's codebook read from the radio's
# SPI flash instead of linked in (30 KB of MCU flash free; docs/k6-spi-codebook.md). A radio
# needs the codebook installed once first: flash out/aura-pager-cbi.bin, boot it (serial
# `K6 CB r=2`), then flash aura-pager-lr-cbs. The images with the table linked (aura-pager-only,
# v3.7, and aura-pager-lr, sotto 1200) work on any radio, installed or not: they are the fallback.
#
# Steps: a fresh clone of Aura at the commit the patch was written against (into
# firmware/k6-aura/build/aura, which this script owns and deletes), git apply
# aura-pager.patch, build sotto's receive library with tools/k6_build.sh (clang + ld.lld;
# or set SOTTO_K6_LIB to a prebuilt libsotto_k6.a), cargo build --release, then the flash
# and RAM report against the KD32F328's 120 KB / 17,720 bytes for the application.
#
# Needs: git, rustup (Aura's rust-toolchain.toml pulls stable + thumbv6m-none-eabi), clang and
# ld.lld 15 or newer for the C library, and llvm-objcopy / llvm-size (or cargo-binutils).
# The patch is GPL-3.0 like Aura; sotto's C library it links is MIT.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
AURA_REPO=${AURA_REPO:-https://github.com/sophiel-meow/bf-k61-firmware-aura}
AURA_REV=edf3b98c3528f909b0e0f4dda80142a70c194ff0     # master, 2026-11-02
WORK="$HERE/build/aura"
OUT="$HERE/out"

rm -rf "$WORK"
mkdir -p "$HERE/build" "$OUT"
git clone -q "$AURA_REPO" "$WORK"
git -C "$WORK" checkout -q "$AURA_REV"

SIZE=${SIZE:-llvm-size}
command -v "$SIZE" >/dev/null 2>&1 || SIZE="rust-size"
NM=${NM:-llvm-nm}
command -v "$NM" >/dev/null 2>&1 || NM="rust-nm"
ELF="$WORK/target/thumbv6m-none-eabi/release/aura"

# flash: everything loaded (vector table, text, rodata, the .data image); RAM: .data + .bss + .uninit
fit() {
  "$SIZE" -A "$1" | awk '
    $1 == ".vector_table" || $1 == ".text" || $1 == ".rodata" || $1 == ".gnu.sgstubs" { flash += $2 }
    $1 == ".data" { flash += $2; ram += $2 }
    $1 == ".bss" || $1 == ".uninit" { ram += $2 }
    $1 == ".ram0" { ram0 += $2 }
    END { printf "flash %d of 122880 bytes (%d free); static RAM %d of 17720 (%d left for the stack); RAM0 %d of 4096\n",
            flash, 122880 - flash, ram, 17720 - ram, ram0
          if (17720 - ram < 12288) print "warning: under 12 KB of stack left (on the radio Aura had used 8.3 KB of it when playback started; the decoder needs 3.5 KB more)"
          if (flash > 122880) { print "DOES NOT FIT in the 120 KB application flash"; exit 1 } }'
}
# where the flash goes: demangled symbols grouped by Rust module path (C symbols by name)
breakdown() {
  "$NM" --print-size --size-sort --demangle --radix=d "$1" | awk '
    $3 ~ /^[tTrRdD]$/ {
      size = $2 + 0; name = $4; for (i = 5; i <= NF; i++) name = name " " $i
      gsub(/^[<_]+/, "", name); g = name
      if (index(name, "::")) { n = split(name, p, "::"); g = p[1] "::" p[2]; if (p[1] == "aura" && n > 3) g = g "::" p[3] }
      else g = "(C) " name
      sum[g] += size; total += size
    }
    END { for (g in sum) printf "%8d  %s\n", sum[g], g; printf "%8d  TOTAL (symbols with sizes)\n", total }' \
    | sort -rn | head -45
}

# The build ID every pager image reports (serial `K6 BOOT build=` at power-on and in each
# `K6 CFG`; its commit on the VOICE MSG screen): sotto's commit, -dirty if tracked files are
# changed, and the UTC build time. CI passes the PR head as SOTTO_SHA; SOTTO_BUILD overrides all.
if [ -z "$SOTTO_BUILD" ]; then
  if [ -n "$SOTTO_SHA" ]; then
    SHA=$(printf %.7s "$SOTTO_SHA")
  else
    SHA=$(git -C "$ROOT" rev-parse --short=7 HEAD 2>/dev/null || echo nogit)
    [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ] && SHA="$SHA-dirty"
  fi
  SOTTO_BUILD="$SHA-$(date -u +%Y%m%dT%H%MZ)"
fi
export SOTTO_BUILD
echo "$SOTTO_BUILD" > "$OUT/build-id.txt"
echo "build ID $SOTTO_BUILD"

# Baseline: stock Aura at the same commit, for comparison (AURA_BASELINE=0 skips it)
if [ "${AURA_BASELINE:-1}" = 1 ]; then
  (cd "$WORK" && cargo build --release)
  { echo "== stock Aura $AURA_REV"; fit "$ELF"; breakdown "$ELF"; } | tee "$OUT/size-aura-stock.txt"
fi

git -C "$WORK" apply "$HERE/aura-pager.patch"

if [ -n "$SOTTO_K6_LIB" ]; then
  LIB=$SOTTO_K6_LIB
else
  sh "$ROOT/tools/k6_build.sh"
  LIB="$ROOT/c/k6/build/libsotto_k6.a"
fi
mkdir -p "$WORK/lib"
cp "$LIB" "$WORK/lib/libsotto_k6.a"

# Link with the flash region opened up, so an overflow still links and shows where the bytes go.
# The layout doesn't depend on the region's length (same origin), so the fit check is the same
# test the linker makes, and no image is written unless it passes.
sed -i 's/LENGTH = 120K/LENGTH = 1024K/' "$WORK/memory.x"

# variant NAME TITLE [cargo args]: build, report to out/size-NAME.txt, write out/NAME.bin if it fits
variant() {
  name=$1; title=$2; shift 2
  (cd "$WORK" && cargo build --release "$@")
  { echo "== $title"; fit "$ELF"; breakdown "$ELF"; "$SIZE" -A "$ELF"; } > "$OUT/size-$name.txt" || true
  cat "$OUT/size-$name.txt"
  fit "$ELF" > /dev/null || return 1
  cp "$ELF" "$OUT/$name.elf"
  if command -v llvm-objcopy >/dev/null 2>&1; then
    llvm-objcopy -O binary "$ELF" "$OUT/$name.bin"
  else
    (cd "$WORK" && cargo objcopy --release "$@" -- -O binary "$OUT/$name.bin")
  fi
  ls -l "$OUT/$name.bin"
  (cd "$OUT" && sha256sum "$name.bin" | tee "$name.bin.sha256")
  # stack frames, largest first (the K6's stack ran out at playback)
  if command -v llvm-objdump >/dev/null 2>&1; then
    llvm-objdump -d -C --no-show-raw-insn "$ELF" | python3 "$ROOT/tools/k6_frames.py" 40 > "$OUT/frames-$name.txt" &&
      { echo "== stack frames, $title"; cat "$OUT/frames-$name.txt"; python3 "$ROOT/tools/k6_frames.py" --sizes "$ELF" | tee -a "$OUT/frames-$name.txt"; }
  fi
}

# Full Aura with VOICE MSG in its launcher: 58 KB too big at edf3b98, so this is a report only
# (FULL=0 skips it). The images to flash are the pager-only ones below.
if [ "${FULL:-1}" = 1 ]; then
  variant aura-pager "Aura + pager (all of Aura)" || echo "(all of Aura + pager doesn't fit: report only)"
fi
# Fallback images with the codebook table linked (TABLE=0 skips them): v3.7 ...
if [ "${TABLE:-1}" = 1 ]; then
variant aura-pager-only "pager-only image (Aura drivers + VOICE MSG)" --features pager-only
fi

# ... and sotto 1200 (LR=0 skips it): the same pager-only build linked with the sotto 1200
# library (plays magic 0xA9 messages; c/k6/sotto_pager.h, SP_LR)
if [ "${TABLE:-1}" = 1 ] && [ "${LR:-1}" = 1 ] && [ -z "$SOTTO_K6_LIB" ]; then
  K6_CODEC=1200 sh "$ROOT/tools/k6_build.sh"
  cp "$ROOT/c/k6/build-lr/libsotto_k6.a" "$WORK/lib/libsotto_k6.a"
  variant aura-pager-lr "pager-only image, sotto 1200" --features pager-only
fi

# sotto's codebook in the K6's SPI flash (CB=0 skips these; c/k6/sp_cb.h, docs/k6-spi-codebook.md).
# v3.7 and sotto 1200 share the codebook, so one image (out/sotto-cb.bin) serves both:
#   aura-pager-cbi     v3.7 with the table linked: at power-on it writes sotto-cb.bin to the SPI
#                      flash at 0x100000 if it isn't there yet (once), then plays from the flash
#                      and times 1000 reads (serial `K6 CB ...` lines). Flash this one first.
#                      (sotto 1200 with the table and the installer is 0.8 KB too big.)
#   aura-pager-cbs     v3.7 without the 30 KB table: reads the codebook the cbi image wrote
#   aura-pager-lr-cbs  sotto 1200 without the table: THE DEFAULT IMAGE
if [ "${CB:-1}" = 1 ] && [ -z "$SOTTO_K6_LIB" ]; then
  rm -f "$OUT/sotto-cb.bin"
  for v in cbi cbs lr-cbs; do
    case $v in cbi) mode=install ;; *) mode=spi ;; esac
    case $v in lr-*) codec=1200 ;; *) codec=2400 ;; esac
    K6_CODEC=$codec K6_CB=$mode sh "$ROOT/tools/k6_build.sh"
    cp "$ROOT/c/k6/build-$v/libsotto_k6.a" "$WORK/lib/libsotto_k6.a"
    variant aura-pager-$v "pager-only image, $([ $codec = 1200 ] && echo "sotto 1200" || echo v3.7), codebook in SPI flash ($mode)" \
      --features pager-only
    if [ -f "$OUT/sotto-cb.bin" ]; then cmp "$ROOT/c/k6/build-$v/sotto-cb.bin" "$OUT/sotto-cb.bin"
    else cp "$ROOT/c/k6/build-$v/sotto-cb.bin" "$OUT/sotto-cb.bin"; fi
  done
  (cd "$OUT" && sha256sum sotto-cb.bin | tee sotto-cb.bin.sha256)
fi

# The hello-world RAM module's code (docs/k6-modules.md), built as modem/k6mod.py builds it, for
# `python -m modem.k6 mod hello.wav out/mod-hello.bin --key-file ...` on a machine without clang.
# Linked at two addresses: the same bytes, or it isn't position-independent.
MODF="--target=thumbv6m-none-eabi -mcpu=cortex-m0 -mthumb -mfloat-abi=soft -Os -fropi -ffreestanding
  -fno-builtin -nostdlib -ffunction-sections -fdata-sections -Wall -Werror -I$ROOT/c/k6"
clang $MODF -c "$ROOT/c/k6/mod/hello.c" -o "$HERE/build/hello.o"
for base in 0 4096; do
  ld.lld -T "$ROOT/c/k6/mod/module.ld" --defsym MODULE_BASE=$base --gc-sections --no-undefined \
    "$HERE/build/hello.o" -o "$HERE/build/hello-$base.elf"
  llvm-objcopy -O binary --only-section=.text "$HERE/build/hello-$base.elf" "$HERE/build/hello-$base.bin"
done
cmp "$HERE/build/hello-0.bin" "$HERE/build/hello-4096.bin"
cp "$HERE/build/hello-0.bin" "$OUT/mod-hello.bin"
ls -l "$OUT/mod-hello.bin"
