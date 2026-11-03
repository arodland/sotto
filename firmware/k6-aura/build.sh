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
    END { printf "flash %d of 122880 bytes (%d free); static RAM %d of 17720 (%d left for the stack)\n",
            flash, 122880 - flash, ram, 17720 - ram
          if (17720 - ram < 6144) print "warning: under 6 KB of stack left (the decoder alone needs 3.5 KB)" }'
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

# Measure with the flash region opened up, so an overflow still links and shows where the bytes
# go; the layout doesn't depend on the region's length. Then link for real against 120 KB.
cp "$WORK/memory.x" "$WORK/memory.x.real"
sed -i 's/LENGTH = 120K/LENGTH = 1024K/' "$WORK/memory.x"
(cd "$WORK" && cargo build --release)
{ echo "== Aura + pager"; fit "$ELF"; breakdown "$ELF"; } | tee "$OUT/size.txt"
"$SIZE" -A "$ELF" >> "$OUT/size.txt"
mv "$WORK/memory.x.real" "$WORK/memory.x"
if ! (cd "$WORK" && cargo build --release); then
  echo "DOES NOT FIT in the 120 KB application flash: see $OUT/size.txt" | tee -a "$OUT/size.txt"
  exit 1
fi
cp "$ELF" "$OUT/aura-pager.elf"

if command -v llvm-objcopy >/dev/null 2>&1; then
  llvm-objcopy -O binary "$ELF" "$OUT/aura-pager.bin"
else
  (cd "$WORK" && cargo objcopy --release -- -O binary "$OUT/aura-pager.bin")
fi
fit "$ELF" | tee -a "$OUT/size.txt"
ls -l "$OUT/aura-pager.bin"
