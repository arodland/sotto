#!/bin/sh
# Build Aura (the open Rust firmware for the Baofeng UV-K6 / K61 family) with the sotto voice
# pager patched in: images in firmware/k6-aura/out/, ready for Aura's tools/flash.py.
#
#   sh firmware/k6-aura/build.sh      ->  out/aura-pager-only.bin (and size reports)
#
# The images read sotto's codebook from the radio's SPI flash (docs/k6-spi-codebook.md), so
# they carry no 30 KB table. A radio needs out/aura-pager-cbi.bin flashed once first, to write
# the codebook there. TABLE=1 (the default) also builds the -table images, which carry it.
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
  K6_CB=spi sh "$ROOT/tools/k6_build.sh"
  LIB="$ROOT/c/k6/build-cbs/libsotto_k6.a"
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
    # Aura's boot runs main and place_app (the App built on the stack, then moved to its
    # static) at once: their frames must fit the free stack with room for the calls under them
    # and an interrupt, or the boot writes over the top of .bss (aura-pager-vlri at 148 B to
    # spare lost sp_vlr.c's cached CRC that way, 2026-11-07)
    "$SIZE" -A "$ELF" | awk -v f="$OUT/frames-$name.txt" '
      $1 == ".data" || $1 == ".bss" || $1 == ".uninit" { ram += $2 }
      END { while ((getline l < f) > 0) { split(l, w, " "); if (w[2] == "aura::__cortex_m_rt_main" || w[2] == "aura::place_app") boot += w[1] }
            printf "boot stack: main + place_app %d of %d B free (%d to spare)\n", boot, 17720 - ram, 17720 - ram - boot
            if (17720 - ram - boot < 384) print "warning: under 384 B of stack to spare at boot: the boot may overwrite the top of .bss" }' \
      | tee -a "$OUT/size-$name.txt"
  fi
}

# lib NAME: link the library tools/k6_build.sh wrote to c/k6/NAME; the codebook image of each
# SPI build goes to out/sotto-cb.bin, and they must all be the same (v3.7 and sotto 1200 share it);
# likewise sotto 600 / 400's, out/sotto-vlr.bin
lib() {
  cp "$ROOT/c/k6/$1/libsotto_k6.a" "$WORK/lib/libsotto_k6.a"
  for img in sotto-cb.bin sotto-vlr.bin; do
    [ -f "$ROOT/c/k6/$1/$img" ] || continue
    if [ -f "$OUT/$img" ]; then cmp "$ROOT/c/k6/$1/$img" "$OUT/$img"
    else cp "$ROOT/c/k6/$1/$img" "$OUT/$img"; fi
  done
}
rm -f "$OUT/sotto-cb.bin" "$OUT/sotto-vlr.bin"
[ -z "$SOTTO_K6_LIB" ] && lib build-cbs

# Full Aura with VOICE MSG in its launcher: too big at edf3b98, so this is a report only
# (FULL=0 skips it). The image to flash is the pager-only one.
if [ "${FULL:-1}" = 1 ]; then
  variant aura-pager "Aura + pager (all of Aura)" || echo "(all of Aura + pager doesn't fit: report only)"
fi
# sotto's codebook in the K6's SPI flash (c/k6/sp_cb.h, docs/k6-spi-codebook.md), the default:
#   aura-pager-only    v3.7 without the 30 KB table: reads the codebook at SPI flash 0x100000
#   aura-pager-lr      sotto 1200 without the table (LR=0 skips it)
#   aura-pager-cbi     v3.7 with the table linked: at power-on it writes out/sotto-cb.bin to the
#                      SPI flash if it isn't there yet (once), then plays from the flash and
#                      times 1000 reads (serial `K6 CB ...` lines). Flash this one first, once
#                      per radio (INSTALL=0 skips it; sotto 1200 with the table and the
#                      installer is 0.8 KB too big)
#   aura-pager-vlr     sotto 1200, 600 and 400 (magic 0xA9, 0xAC, 0xAD; docs/sotto-vlr.md), both
#                      codebooks in SPI flash: v3.7's at 0x100000 and sotto 600 / 400's 21 KB at
#                      0x108000 (VLR=0 skips it and the next)
#   aura-pager-vlri    the same with sotto 600 / 400's codebooks linked: at power-on it writes
#                      out/sotto-vlr.bin to the SPI flash if it isn't there yet (once; serial
#                      `K6 VLR r=2`, then r=1). Flash it once per radio, after aura-pager-cbi
#   aura-pager-vlr-rom sotto 1200, 600 and 400 with sotto 600 / 400's 21 KB of codebooks in the
#                      MCU's flash, read in place as ROM: it never writes the SPI flash (serial
#                      `K6 VLR r=1`, no install; it still reads v3.7's codebook at 0x100000 for
#                      sotto 1200). Lean, as vlri
#   aura-pager-only-table, aura-pager-lr-table   the table in the MCU's flash, as before: no
#                      SPI flash needed; lean (no module TX, ARQ reports or calibration
#                      writes), with little flash to spare (TABLE=0 skips them)
variant aura-pager-only "pager-only image (Aura drivers + VOICE MSG), v3.7, codebook in SPI flash" \
  --features pager-only
if [ -n "$SOTTO_K6_LIB" ]; then LR=0; INSTALL=0; TABLE=0; VLR=0; EXPLORE=0; fi
if [ "${LR:-1}" = 1 ]; then
  K6_CODEC=1200 K6_CB=spi sh "$ROOT/tools/k6_build.sh"
  lib build-lr-cbs
  variant aura-pager-lr "pager-only image, sotto 1200, codebook in SPI flash" --features pager-only
fi
# aura-explore: EXPLORE, the K6 signal explorer (docs/k6-explore-app.md), as the start screen of
# a pager-only image whose library has no voice decoder (K6_VOICE=0, c/k6/sp_novoice.c): the
# pager's packets, RAM modules (API level 5) and control stay, the voice doesn't play. The
# explorer and its 13 KB of C don't fit beside the decoder (EXPLORE=0 skips it)
if [ "${EXPLORE:-1}" = 1 ]; then
  K6_VOICE=0 K6_CB=spi sh "$ROOT/tools/k6_build.sh"
  lib build-explore
  variant aura-explore "EXPLORE image: the signal explorer, pager packets and modules, no voice" --features explore
fi
if [ "${VLR:-1}" = 1 ]; then
  K6_CODEC=vlr K6_CB=spi sh "$ROOT/tools/k6_build.sh"
  lib build-vlr-cbs
  variant aura-pager-vlr "pager-only image, sotto 1200 + 600 + 400, codebooks in SPI flash" --features pager-only
  K6_CODEC=vlr K6_CB=install sh "$ROOT/tools/k6_build.sh"
  lib build-vlr-cbi
  # it holds sotto 600 / 400's 21 KB of codebooks: lean, as the other install image
  variant aura-pager-vlri "pager-only image, sotto 1200 + 600 + 400, installs sotto 600 / 400's codebooks" \
    --features pager-only,lean
  K6_CODEC=vlr K6_CB=rom sh "$ROOT/tools/k6_build.sh"
  lib build-vlr-rom
  # vlri without the installer: the same 21 KB of tables, never written anywhere
  variant aura-pager-vlr-rom "pager-only image, sotto 1200 + 600 + 400, sotto 600 / 400's codebooks in MCU flash (no SPI writes)" \
    --features pager-only,lean
fi
# the hiss band image (docs/k6-hiss-port.md): sotto 1200, 600 and 400 at 16 kHz through the K6
# hiss layer, codebooks in SPI flash (HISS=0 skips it)
if [ "${HISS:-1}" = 1 ]; then
  K6_CODEC=vlr K6_CB=spi K6_HISS=1 sh "$ROOT/tools/k6_build.sh"
  lib build-vlrh-cbs
  variant aura-pager-vlrh "pager-only image, sotto 1200 + 600 + 400 with the hiss band (16 kHz), codebooks in SPI flash" \
    --features pager-only,hiss
fi
if [ "${INSTALL:-1}" = 1 ]; then
  K6_CB=install sh "$ROOT/tools/k6_build.sh"
  lib build-cbi
  # the install image also holds the 30 KB table: without the PTT calibration transmit (lean)
  variant aura-pager-cbi "pager-only image, v3.7, codebook install (writes it to SPI flash once)" \
    --features pager-only,lean
fi
if [ "${TABLE:-1}" = 1 ]; then
  sh "$ROOT/tools/k6_build.sh"
  lib build
  # lean too since the ARQ reports and API level 4 (2026-11-06): with the 30 KB table they
  # didn't fit, and this fallback needs neither
  variant aura-pager-only-table "pager-only image, v3.7, codebook in MCU flash" --features pager-only,lean
  if [ "${LR:-1}" = 1 ]; then
    K6_CODEC=1200 sh "$ROOT/tools/k6_build.sh"
    lib build-lr
    # module API level 2 took it 44 B over: lean, as the install image
    variant aura-pager-lr-table "pager-only image, sotto 1200, codebook in MCU flash" --features pager-only,lean
  fi
fi
[ -f "$OUT/sotto-cb.bin" ] && (cd "$OUT" && sha256sum sotto-cb.bin | tee sotto-cb.bin.sha256)
[ -f "$OUT/sotto-vlr.bin" ] && (cd "$OUT" && sha256sum sotto-vlr.bin | tee sotto-vlr.bin.sha256)

# The RAM modules' code (docs/k6-modules.md: hello; settle, cwprobe, cwlisten and i2sprobe, level 2; cwkey and
# ft8tx, level 3; ifcal, level 4; rxmap, level 2, its parameter block patched by tools/k6_rxmap.py; scan, level 2, by tools/k6_scan.py; hfprobe and bandreg, level 2, by tools/k6_hf.py), built as
# modem/k6mod.py builds them, for `python -m modem.k6 mod hello.wav out/mod-hello.bin --key-file ...`
# (`--api-level 2` for the level 2 ones) on a machine without clang. A module
# that says #define K6_MODULE_OPT z gets -Oz instead of -Os. Linked at two addresses: the
# same bytes, or it isn't position-independent.
MODF="--target=thumbv6m-none-eabi -mcpu=cortex-m0 -mthumb -mfloat-abi=soft -Os -fropi -ffreestanding
  -fno-builtin -nostdlib -ffunction-sections -fdata-sections -fomit-frame-pointer -Wall -Werror -I$ROOT/c/k6"
for m in hello settle cwprobe cwlisten i2sprobe i2scap gp0tone gp0fm gp0raw gp0dma gp0sweep afsweep gp0cap \
         iflive iflive_ffsk iflive_tsweep iflive_fsweep iflive_g2 iflive_k6t iflive_h1200 iflive_h1350 iflive_h1440 iflive_h1500 iflive_h1600 xtalstep cwkey ft8wf ft8wf_inv gp0cap_afc ft8tx ifcal rxmap spksweep scan hfprobe rtls wsprtx wsprbcn wspriq bandreg ramprobe; do
  opt=$(sed -n 's/^[[:space:]]*#[[:space:]]*define[[:space:]]*K6_MODULE_OPT[[:space:]]*\([sz]\).*/\1/p' "$ROOT/c/k6/mod/$m.c")
  clang $(echo $MODF | sed "s/ -Os / -O${opt:-s} /") -c "$ROOT/c/k6/mod/$m.c" -o "$HERE/build/$m.o"
  for base in 0 4096; do
    ld.lld -T "$ROOT/c/k6/mod/module.ld" --defsym MODULE_BASE=$base --gc-sections --no-undefined \
      "$HERE/build/$m.o" -o "$HERE/build/$m-$base.elf"
    llvm-objcopy -O binary --only-section=.text "$HERE/build/$m-$base.elf" "$HERE/build/$m-$base.bin"
  done
  cmp "$HERE/build/$m-0.bin" "$HERE/build/$m-4096.bin"
  cp "$HERE/build/$m-0.bin" "$OUT/mod-$m.bin"
  ls -l "$OUT/mod-$m.bin"
  # the 4 KB message store holds a 16-byte header, the code and an 8-byte tag
  [ "$(wc -c < "$OUT/mod-$m.bin")" -le 4072 ] || { echo "mod-$m.bin: over the 4 KB store"; exit 1; }
done
