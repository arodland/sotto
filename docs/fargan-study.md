# sotto against a neural vocoder (FARGAN)

*2026-11-02.* Could a neural vocoder replace or enhance sotto's decoder on the radios sotto
targets? We tested FARGAN, the neural vocoder in Opus 1.5 and later
([IEEE paper](https://ieeexplore.ieee.org/document/10632624)), and applied what sotto learned about the 4-8 kHz hiss band. This is similar in spirit to the M17
project's Codec 2 neural enhancer tests.

**Result.** Stock FARGAN decoding the unchanged sotto v3.8 bitstream sounds and scores about the
same as sotto's own decoder (wideband PESQ 1.77 against 1.78 over 10 voices). It costs about 305
million multiply-adds per second and 811 KB of weights. sotto's decoder runs in 55 MHz on the
FTM-300D's C5517 DSP and 36 MHz on the MD-UV380's Cortex-M4, with under 100 KB of tables and
state. FARGAN fits neither radio. The hiss band helps FARGAN too: without sotto's hiss index it
drops to 1.63. For the bits a 2400 bit/s radio link can carry, sotto's decoder gets as much out of
them as a stock neural vocoder does, at a fraction of the compute and memory, on hardware people
already own.

Listen: the [main page](https://baitisj.github.io/sotto/), "Part 4". Code: `tools/fargan_study.py`.
Scores: `docs/results/fargan_study.json`.

## What FARGAN costs

FARGAN makes 16 kHz speech from 20 numbers per 10 ms: 18 cepstral coefficients over Bark bands up
to 8 kHz, a pitch period and a pitch correlation. A conditioning net runs every 10 ms. The signal
net (three GRUs and gated layers) runs every 2.5 ms and writes 40 samples. These counts come from
the weight shapes in Opus 1.6.1's `dnn/fargan_data.c` and the call order in `dnn/fargan.c`.

| Part | Runs per second | int8 weights | Million multiply-adds/s |
|---|---:|---:|---:|
| Conditioning net | 100 | 65,536 | 6.8 |
| Input conv + gate | 400 | 99,840 | 39.9 |
| GRU 1 (160 units) + gate | 400 | 232,960 | 93.2 |
| GRU 2 (128) + gate | 400 | 157,696 | 63.1 |
| GRU 3 (128) + gate | 400 | 145,408 | 58.2 |
| Skip dense + gate + output | 400 | 109,568 | 43.8 |
| **Total** | | **811,008** (+ about 9 K float) | **about 305** |

| Target | Room | FARGAN needs | Fits? |
|---|---|---|---|
| Yaesu FTM-300D: TI C5517, 200 MHz | 320 KB RAM in all | 811 KB of weights; about 305 MHz at one multiply-add per cycle (int8 weights must be unpacked before the C55x's dual MACs can use them) | No: 2.5x over memory, about 2x over compute |
| TYT MD-UV380 (OpenRTX, M17): STM32F405 Cortex-M4, 168 MHz | 1 MB flash shared with the firmware, 192 KB RAM | 811 KB of flash; about 305 MHz at one int8 multiply-add per cycle | No: about 2x over compute, flash nearly full |
| Raspberry Pi, phone, PC, browser | GHz-class cores with SIMD | | Yes: 15 s of speech in 0.14 s on one 2.1 GHz Xeon core |

Every 2.5 ms the signal net reads all of its weights once, so they must sit in fast memory.
Streaming them from external flash would take about 300 MB/s. A model sized for the radios would
have about 150 K weights and about 60 million multiply-adds per second, a fifth of FARGAN. Opus's
LACE post-filter (268 K int8 weights, about 30-35 million multiply-adds per second) is another
shape that might shrink to fit. Either would have to be trained from scratch for sotto, which
takes a GPU. That is outside this project, which targets existing radios.

## Where the hiss band fits

FARGAN's top five bands are centred at 4.0, 4.8, 5.6, 6.8 and 8.0 kHz. sotto v3.8's 6-bit hiss
index codes four band levels from 4 to 8 kHz, so it maps onto them almost one to one. A decoder
that has no hiss index has to guess those bands from the lower ones. Our own measurement put such
a guess 11-14 dB off on unvoiced frames, against about 6 dB with the index
([unvoiced-hiss.md](unvoiced-hiss.md)).

## Results

Ten clean 16 kHz voices (the page's eight held-out LibriSpeech readers plus readers 198 and 3436),
no radio channel, the same v3.8 bitstreams the page decodes. WB PESQ hears 0-8 kHz, NB PESQ 0-4 kHz.

| Decoder | WB PESQ | NB PESQ | ESTOI |
|---|---:|---:|---:|
| sotto v3.8 (C decoder) | 1.78 | 2.85 | 0.81 |
| FARGAN on v3.8, hiss index used | 1.77 | 2.77 | 0.79 |
| FARGAN on v3.8, 4-8 kHz guessed | 1.63 | 2.78 | 0.79 |
| FARGAN on v3.8, pitch from sotto's bits | 1.57 | 2.31 | 0.77 |
| FARGAN from the original's features (not sendable at 2400 bit/s) | 2.72 | 3.47 | 0.89 |

- **FARGAN on v3.8** takes its features from v3.8's 16 kHz output with Opus's own analysis, so
  FARGAN's top bands carry the hiss index.
- **4-8 kHz guessed** replaces FARGAN's top five bands with a ridge regression from the lower
  bands, trained on the other nine voices' originals.
- **Pitch from sotto's bits** feeds sotto's own pitch and voicing. It does worse, probably because
  stock FARGAN expects its own pitch estimator's correlation values.
- **The original's features** are 20 full-precision numbers every 10 ms. The bottom row shows how
  good the vocoder is, not what a 2400 bit/s codec could send.

**Where the loss is.** Starting from "FARGAN on v3.8", the original's value of one feature group
at a time:

| Original value swapped in | WB PESQ | NB PESQ | ESTOI |
|---|---:|---:|---:|
| 0-4 kHz envelope (bands 0-12) | 2.13 | 3.10 | 0.86 |
| 4-8 kHz bands (13-17) | 1.89 | 2.74 | 0.80 |
| Pitch period | 1.85 | 2.89 | 0.80 |
| Pitch correlation | 1.80 | 2.81 | 0.79 |

The hiss index closes about half the gap between a blind guess and perfect 4-8 kHz bands (0.14
of 0.26 WB PESQ). The largest remaining loss is the 34-bit envelope quantizer, which no decoder
can recover without being retrained on sotto's quantized features. WB PESQ penalises hiss even at
the right level (listening round 11), so listening decides between close rows.

## Licenses

Opus's code, its trained weights and its training scripts are under Opus's 3-clause BSD license,
compatible with sotto's MIT license. This repo does not include Opus; `tools/fargan_study.py`
runs a separately built `fargan_demo`. The FARGAN clips on the page are FARGAN's output from
LibriSpeech readers (CC BY 4.0). We have not checked whether any patent covers FARGAN.

## Reproduce

1. Build sotto (`make`) and Opus 1.6.1 with its weights. The release tarball has them; so does
   the crates.io package `opusic-sys` 0.7.6, which bundles that tarball. Run
   `sh ./configure --enable-dred --enable-osce --disable-shared && make && make fargan_demo`.
2. `python tools/fargan_study.py --demo PATH/fargan_demo --flac male_libri3436_15s female_libri198_14s`
   (Python 3.11: numpy, scipy, soundfile, pesq, pystoi). About 80 s on one core.
