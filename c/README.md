# sotto in C

A C port of the sotto codec (`sim/vocoder_lab.py`, `sim/hiss_band.py`), floating point. It
builds v3.8 by default (`SottoV38`: v3.7's frames plus the 4-8 kHz hiss band, 16 kHz in and
out, `sotto_hiss.c`), v3.7 (`SottoV37`, 8 kHz, the 29 KB codebook for the radio's DSP) with
`make VARIANT=v3.7`, and v3.6 and v3.5 likewise. It encodes speech into 48-bit frames every
20 ms (2400 bit/s) and decodes them back, with the same bits as the Python model.

The port reuses Codec 2's sinusoidal analysis and synthesis by linking `libcodec2` (LGPL-2.1,
built from `codec/codec2/` by `tools/build_codec2.sh`). The code in this directory is MIT.

## Build and run

The top-level `make` does the first two steps (see the main README). By hand:

```sh
tools/build_codec2.sh          # once: builds build/codec2/src/libcodec2.so
make -C c                      # no Python needed: the generated tables are committed
make -C c check                # API test against libsotto.so
c/sotto_enc -16 in.raw out.bit   # 16 kHz, 16-bit mono raw in; 6 bytes per frame out (v3.8)
c/sotto_dec -16 out.bit dec.raw [good.u8]
c/sotto_enc in.raw out.bit       # without -16: the 8 kHz v3.7 encoder and decoder
c/sotto_dec out.bit dec.raw [good.u8]
c/sotto_enc -16 -1200 in.raw out.bit   # sotto 1200 hiss: 16 kHz in, 6 bytes per 40 ms
c/sotto_dec -16 -1200 out.bit dec.raw [good.u8]   # (-1200 alone: sotto 1200 at 8 kHz)
c/sotto_enc -16 -600 in.raw out.bit    # sotto 600 hiss: 16 kHz in, 6 bytes per 80 ms (-400: 120 ms)
c/sotto_dec -16 -600 out.bit dec.raw [good.u8]    # (plain sotto 600 / 400: fixed/sotto_vlr_dec_fx)
make -C c install PREFIX=/usr/local
```

The build produces `libsotto.so` (soname `libsotto.so.0`, version 0.1.0), `libsotto.a`, the
header `sotto.h` and a pkg-config file (`pkg-config --cflags --libs sotto`). The shared
library exports only the `sotto_*` functions in `sotto.h`. It needs `libcodec2` at run time:
the build-tree copy is found through an rpath, and an installed copy through the system's
library path. `sotto_format()` names the bitstream the library speaks, for example
`sotto v3.8, shape 9-9-8-8`. A library built with another codebook can't decode its frames.
`make -C c RULE_LAG=5` trades the v3.8 encoder's look-ahead (frames, default 10) for
latency; `docs/c-port.md` has the table.

`sotto_codebook.c`, `sotto_hiss_tables.c` and `fixed/fx_tables.c` are generated from
`sim/data/*.npz` by `tools/export_codebook_c.py`, `tools/export_hiss_c.py` and
`tools/gen_fx_tables.py`, and committed so that the build needs no Python. `make -C c tables`
rewrites them (needs numpy and scipy); `make -C c check-tables` fails if the committed copies
differ from what the generators write now. `VARIANT=v3.5` or another `CODEBOOK=` generates its
own `sotto_codebook_gen.c` at build time, with Python and numpy.

## Fixed point

`make -C c fixed` builds `c/fixed/sotto_enc_fx` and `c/fixed/sotto_dec_fx`, an integer-only
encoder and decoder for the radio's DSP with the same API (`c/fixed/sotto_fx_enc.c`,
`c/fixed/sotto_fx_dec.c`, and the v3.8 16 kHz layer in `c/fixed/sotto_fx_hiss.c`, so the
tools take `-16` as the float ones do). Their tables are the committed `c/fixed/fx_tables.c`, from `tools/gen_fx_tables.py`.
`tools/check_fx.py` compares them with the float port (`--enc` for the encoder, `--v38` for
the 16 kHz layer), and `tools/fx_profile.sh` counts their inner loops. Results and the C55x
measurements are in `docs/c-port.md`.

## Input level

`sotto.h` documents the level scale and gives it as constants. Samples are 16-bit, and
levels are dBFS of RMS, so a full-scale sine is −3 dBFS. The 5-bit energy field covers
frames from −76 dBFS (quieter frames decode as silence) to −7.3 dBFS (louder frames
saturate). Aim for an active speech level near −26 dBFS (`SOTTO_LEVEL_TARGET_DBFS`): the test
voices sit at −22 to −27 dBFS, and their loudest 10 ms are 3 to 8 dB below saturation. The
decoder reproduces the coded level. A pytest case checks the constants against the encoder.

`good.u8` is optional: one byte per frame, 0 for a frame that failed its CRC. The decoder
conceals those frames as v3.5 and v3.6 do (hold 40 ms, fade 6 dB per 20 ms, silent after 80 ms).

## API

See `sotto.h`. The 8 kHz encoder takes 160 samples per call and returns a frame once its
pitch tracker has 80 ms of look-ahead (the first frame comes on the 6th call);
`sotto_enc_flush` returns the rest at the end of the stream. The 8 kHz decoder returns 160
samples per frame, 20 ms late, because each odd 10 ms half is interpolated with the next
frame. The v3.8 pair (`sotto_enc16_*`, `sotto_dec16_*`) works in 320-sample blocks at
16 kHz: the encoder's first frame comes on the 17th call (the voicing rules look
`SOTTO_RULE_LAG` frames ahead), and the decoder's output is `SOTTO_DEC16_DELAY_FRAMES`
(5) frames late, with `sotto_dec16_flush` draining them at the end.
sotto 1200 hiss (`sotto_lr_enc16_*`, `sotto_lr_dec16_*`, `docs/sotto-1200-hiss.md`) works in
640-sample blocks at 16 kHz, one 48-bit superframe each: the encoder's first comes on the
12th call, and the decoder plays `SOTTO_LR_DEC16_DELAY` (4) superframes late.
`tools/check_lr_hiss.py` checks it against `sim/sotto_lr_hiss.py`. sotto 600 / 400 hiss
(`sotto_vlr_enc16_*`, `sotto_vlr_dec16_*` in `sotto_vlr.h`, `docs/sotto-vlr.md`) takes 10 ms
blocks at 16 kHz and pops superframes as they are ready (about 10 superframes late at 600, 9
at 400); the decoder plays `SOTTO_VLR_DEC16_DELAY(rate)` (4 or 3) superframes late.
`tools/check_vlr_hiss.py` checks it against `sim/sotto_vlr_hiss.py`. The fixed-point build has
no sotto 1200 or 600 / 400 hiss yet (their create functions return NULL).

## Checked against Python

`tools/check_c_port.py` encodes clips with both, and decodes the same bits with both (with a
random 10% frame loss). On 19 voices with v3.5 (`docs/results/c_port_check.json`):

- encoder: 11,170 of 11,173 frames (99.97%) bit-exact. The 3 others are near-ties where the
  pitch tracker can go either way between two octaves: the C side computes spectra with
  Codec 2's float FFT, the Python side in double precision. Mean PESQ is 2.472 for the C
  encoder against 2.475 for Python (with the 10% loss).
- decoder: given the same bits, the C output matches the Python output to about 100 dB SNR,
  concealment included.
- v3.6 (`docs/results/c_port_check_v36.json`): the same 11,170 of 11,173 frames bit-exact,
  decoders within 96 dB.
- v3.7 (`docs/results/c_port_check_v37.json`): again 11,170 of 11,173 frames bit-exact,
  decoders within 98 dB.
- v3.8 (`tools/check_c_port38.py`, `docs/results/c_port38_lag10.json`): bit-exact with the
  Python on five 7 s clips but one shape near-tie; the hiss band's level per frame agrees as
  well as two Python seeds do. `docs/c-port.md` has the look-ahead sweep.

## Speed

On one x86 core, the encoder runs at 3% of real time and the decoder at 0.1%. The shape
vector quantizer's M-best search is 90% of the encoder (about 700,000 multiply-adds per
frame with the 12+11+11 codebook). See `docs/c-port.md` for the memory and cycle budget on
the radio's TI C5517 DSP.
