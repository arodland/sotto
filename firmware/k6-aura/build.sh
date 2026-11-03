#!/bin/sh
# Build Aura (the open Rust firmware for the Baofeng UV-K6 / K61 family) with the sotto voice
# pager patched in: firmware/k6-aura/out/aura-pager.bin, ready for Aura's tools/flash.py.
#
#   sh firmware/k6-aura/build.sh
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
git -C "$WORK" apply "$HERE/aura-pager.patch"

if [ -n "$SOTTO_K6_LIB" ]; then
  LIB=$SOTTO_K6_LIB
else
  sh "$ROOT/tools/k6_build.sh"
  LIB="$ROOT/c/k6/build/libsotto_k6.a"
fi
mkdir -p "$WORK/lib"
cp "$LIB" "$WORK/lib/libsotto_k6.a"

(cd "$WORK" && cargo build --release)
ELF="$WORK/target/thumbv6m-none-eabi/release/aura"
cp "$ELF" "$OUT/aura-pager.elf"

if command -v llvm-objcopy >/dev/null 2>&1; then
  llvm-objcopy -O binary "$ELF" "$OUT/aura-pager.bin"
else
  (cd "$WORK" && cargo objcopy --release -- -O binary "$OUT/aura-pager.bin")
fi

SIZE=${SIZE:-llvm-size}
command -v "$SIZE" >/dev/null 2>&1 || SIZE="rust-size"
"$SIZE" -A "$ELF" | tee "$OUT/size.txt"
# flash: everything loaded (vector table, text, rodata, the .data image); RAM: .data + .bss + .uninit
"$SIZE" -A "$ELF" | awk '
  $1 == ".vector_table" || $1 == ".text" || $1 == ".rodata" { flash += $2 }
  $1 == ".data" { flash += $2; ram += $2 }
  $1 == ".bss" || $1 == ".uninit" { ram += $2 }
  END {
    printf "flash %d of 122880 bytes (%d free); static RAM %d of 17720 (%d left for the stack)\n",
      flash, 122880 - flash, ram, 17720 - ram
    if (flash > 122880) { print "DOES NOT FIT: flash"; exit 1 }
    if (17720 - ram < 6144) print "warning: under 6 KB of stack left (the decoder alone needs 3.5 KB)"
  }' | tee -a "$OUT/size.txt"
ls -l "$OUT/aura-pager.bin"
