# sotto

Open 2400 bps digital voice for FM radios: a vocoder, channel coding and a
constant-envelope modem path, built to run on small fixed-point DSPs (first
target: the TI C5517 in the Yaesu FTM-300D).

## Listen

Compare sotto with Codec 2 and with Fusion-style protection over a simulated weak mobile signal, for male and female voices: **https://baitisj.github.io/sotto/** (the page is `docs/index.html`; the FLAC files are in `docs/samples/`). The page decodes its sotto v3.8 clips in your browser from the received bits, with the C decoder compiled to WebAssembly (`c/wasm/`).

## Build libsotto

You need a C compiler, `make` and `cmake`. Python is not needed: the C tables generated from
`sim/data/` (codebook, hiss band, fixed-point tables) are committed. From the top of the
repository:

```sh
make                      # Codec 2 fork into build/codec2, then libsotto and the tools in c/
make check                # C API test, table check, then the Python tests (needs the Python dependencies below)
sudo make install         # PREFIX=/usr/local by default; then run sudo ldconfig on Linux
```

`make` produces `c/libsotto.so` (soname `libsotto.so.0`), `c/libsotto.a`, and two command-line
tools that read and write raw files:

```sh
sox talk.wav -r 8000 -c 1 -b 16 -e signed -t raw talk.raw   # 8 kHz, 16-bit mono
c/sotto_enc talk.raw talk.bit                     # 6 bytes per 20 ms frame (2400 bit/s)
c/sotto_dec talk.bit out.raw
```

After `make install`, build a program against it with
`cc app.c $(pkg-config --cflags --libs sotto)` and `#include <sotto.h>`. The header documents
the API and the input level the codec expects (speech near −26 dBFS).

- `make install` also installs `libcodec2` and its headers from our fork (`codec/codec2/`,
  LGPL-2.1), because libsotto calls Codec 2's analysis and synthesis. To keep it apart from a
  Codec 2 already on your system, install to its own prefix, for example
  `make install PREFIX=/opt/sotto`, and set `PKG_CONFIG_PATH=/opt/sotto/lib/pkgconfig` and
  `LD_LIBRARY_PATH=/opt/sotto/lib`.
- The committed `c/sotto_codebook.c`, `c/sotto_hiss_tables.c` and `c/fixed/fx_tables.c` are
  written by `tools/export_codebook_c.py`, `tools/export_hiss_c.py` and `tools/gen_fx_tables.py`.
  After changing a generator or its `.npz` data, run `make tables` (needs numpy and scipy) and
  commit the result; `make check-tables` (also part of `make check` and CI) fails when they
  disagree. `make PYTHON=.venv/bin/python` picks the Python for these targets.
- The library speaks one bitstream, v3.6 by default (`sotto_format()` says which). Its
  encoder is v3.7, which only tracks pitch better, so v3.6 and v3.7 frames are the same
  format. `make clean all VARIANT=v3.6` builds v3.6's encoder, `VARIANT=v3.5` the older
  bitstream (its codebook is generated at build time, so it needs Python with numpy).
- `make distclean` also removes the Codec 2 build. More detail is in `c/README.md`.

## Contents

- `modem/`: the **softmodem**. It sends and receives YSF frames carrying sotto voice through a radio's 9600 data port and a sound card, or through an SDR (IQ). `python -m modem tx voice.bit tx.flac`, then `python -m modem rx rx.flac voice.bit`. See `docs/softmodem.md`.
- `tests/`: `python -m pytest tests`.
- `c/`: the **C port** of the sotto v3.7 encoder and v3.6 decoder (float, links Codec 2), built as `libsotto` by `make` (see Build libsotto above). See `c/README.md` and `docs/c-port.md` for the plan to run it on the radio's DSP.
- `tools/listening/` and `docs/listening-tests.md`: the blind A/B listening-test pages used to check each codec change by ear.
- `tools/build_codec2.sh`: builds libcodec2, c2enc, c2dec and c2sim into `build/codec2/src` (the top-level `make` runs it).
- `sim/`: a C4FM (4-level FSK over FM) link simulator with Rayleigh and two-path
  fading; an FEC race (`fec_race.py`); a waveform race (`waveform_race.py`, `vw_race.py`); and an
  end-to-end speech test through the softmodem (`audio_race.py`, needs the Codec 2 build).
- `tools/score_samples.py`: PESQ (narrowband) and ESTOI scoring of coded clips
  against the originals.
- `codec/codec2/`: our working fork of Codec 2 (LGPL-2.1), where the oracle experiments and the 2400 bps improvements happen.
- `docs/course/`: **How sotto works**, an illustrated beginner course (about 10th-grade level) with a glossary. Figures and demos are rebuilt by `tools/course_figures.py`. Online at https://baitisj.github.io/sotto/course/.
- `docs/`: design memos (TWELP survey, Codec 2 improvement plan, FEC race, waveform race,
  softmodem, audio results).

## Rules

- No third-party code with incompatible licenses (for example the STANAG 4591
  reference code). No Yaesu firmware or images.
- Python dependencies: numpy, scipy, soundfile, pesq, pystoi, pytest (pesq needs Python 3.11 or older to build).
- Audio is stored as 16-bit FLAC. `sim/` writes it with [HamFLAC](https://github.com/baitisj/hamflac) when the `hamflac` binary is on PATH or in `$HAMFLAC`, and with soundfile otherwise.

## License

- Everything outside `codec/` is MIT (see `LICENSE`).
- `codec/codec2/` is a fork of Codec 2 and stays **LGPL-2.1** (see `codec/README.md` and `codec/codec2/COPYING`).
