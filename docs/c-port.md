# sotto on the C5517: C port and budget

*2026-10-31, C55x measurements 2026-11-02. The float C port is in `c/`, and `c/README.md` explains how to build and run it.
This page covers what the radio's DSP needs that the float port doesn't have yet.*

## What exists

- **`c/`** is a float C port of v3.5. It has a streaming encoder whose pitch tracker decides
  each frame 80 ms late (fixed-lag Viterbi). It also has a decoder with v3.5's concealment.
  Both link Codec 2 for the sinusoidal analysis, phase synthesis, postfilter and overlap-add.
- **Check against Python.** On 19 voices, 11,170 of 11,173 frames are bit-exact with
  `SottoV35` running the same 80 ms tracker. Given the same bits, the two decoders agree to
  about 100 dB SNR (`docs/results/c_port_check.json`, from `tools/check_c_port.py`).
  - The 3 frames that differ are octave near-ties in the tracker.
  - The C side uses Codec 2's float FFT and the Python side uses double precision.
- **Delay.**

  | Stage | Delay |
  |---|---|
  | Encoder look-ahead | 90 ms (80 ms tracker + 10 ms for the next frame's pitch candidate) |
  | Frame | 20 ms |
  | Decoder interpolation | 20 ms |

  On the air, you then add the YSF frame (100 ms), plus another 100 ms if you use depth-2
  interleaving.

## Where the time goes

Measured on one x86 core with gprof, 60 s of speech:

| Part | Share of encoder time |
|---|---:|
| Shape VQ, M-best search (12+11+11 bits, M = 8) | 90% |
| Pitch tracker candidates (harmonic-sum refinement, fit error, odd/even check) | 9% |
| Codec 2 analysis (nlp, FFT, refinement, voicing) | under 3% |

The encoder runs at 3% of real time and the decoder at 0.1%.

The VQ search costs about 700,000 multiply-adds per 20 ms frame:
- stage 1 compares the target with 4,096 vectors;
- stages 2 and 3 each compare 8 surviving paths with 2,048 vectors;
- each vector has 19 dimensions.

That is 35 million multiply-adds a second. The C5517 has two 17×17-bit MACs at up to
200 MHz, so in 16-bit fixed point the search would need about 20 to 30 MHz. That is too
much to spend on one block when the C4FM modem has to share the DSP.

## Memory, and a smaller codebook

The C5517 has 320 KB of on-chip RAM: 64 KB dual-access and 256 KB single-access. Yaesu's
modem and whatever replaces AMBE have to share it. The shipped codebook has 155,648 values.
It needs 608 KB as float, 304 KB as 16-bit and 152 KB as 8-bit, so it doesn't fit as it is.

Smaller layouts were trained on the same data (Speech Commands 600 + LibriSpeech 32 + Codec 2
recordings). They keep the 34 bits, so the frame doesn't change. Scores are v3.5 on 19
voices, clean channel, 80 ms tracker:

| Codebook (bits per stage) | Values | 8-bit size | VQ multiply-adds per frame | PESQ | ESTOI |
|---|---:|---:|---:|---:|---:|
| 12 + 11 + 11 (shipped) | 155,648 | 152 KB | 700,000 | 2.917 | 0.80 |
| 12 + 11 + 11, values rounded to 8 bits | 155,648 | 152 KB | 700,000 | 2.908 | 0.80 |
| 12 + 11 + 11, rounded to 6 bits | 155,648 | 114 KB | 700,000 | 2.898 | 0.80 |
| **9 + 9 + 8 + 8** | 29,184 | **29 KB** | **165,000** | **2.913** | 0.80 |
| 10 + 8 + 8 + 8 | 34,048 | 33 KB | 136,000 | 2.915 | 0.80 |
| 9 + 9 + 8 + 8, M = 16 | 29,184 | 29 KB | 321,000 | 2.896 | 0.80 |
| 9 + 9 + 8 + 8, M = 16, rounded to 8 bits | 29,184 | 29 KB | 321,000 | 2.904 | 0.80 |
| 8 + 8 + 6 + 6 + 6 | 13,376 | 13 KB | 73,000 | 2.881 | 0.80 |

Differences under about 0.02 are within the noise of 19 clips. The small layouts were also
trained with Codec 2's recordings, which include the ve9qrp voice. So read the table as
showing that **a codebook a fifth the size loses nothing measurable**, not that one layout
beats another.

- **Codebook for the DSP: 9 + 9 + 8 + 8 with M = 8, stored as 8-bit values.** It needs
  29 KB of RAM and 165,000 multiply-adds per frame, about 4 to 6 MHz on the C5517.
  - It is saved as `sim/data/shape_msvq_9988.npz`.
  - `make -C c CODEBOOK=sim/data/shape_msvq_9988.npz` builds the C port with it. The
    check gives bit-exact frames on 3 voices.
  - The float encoder runs 3 times faster with it.
- Switching codebooks changes the bitstream, so the radio and the PC tools must use the
  same one. The Python default (`SottoV35`) still uses the 12 + 11 + 11 book. The switch
  should be a named version, v3.6, after a listening check.
- **Listening round 9:** v3.5 against the 9 + 9 + 8 + 8 codebook in 8-bit values gave
  5 ties out of 5, on held-out voices with no radio channel (Jeff). The null pair was
  answered correctly. The small codebook can become v3.6.
- Rounding to 8 bits costs about 0.01 PESQ. Rounding to 6 bits costs about 0.02.

## v3.8: the 16 kHz layer

*2026-11-02.* `c/sotto_hiss.c` adds the v3.8 hiss band (`sim/hiss_band.py`, `docs/unvoiced-hiss.md`)
to the float port. `make -C c` builds it by default (`VARIANT=v3.8`); `VARIANT=v3.7` gives the
8 kHz library as before. The frame is v3.7's, so a v3.7 decoder plays a v3.8 stream without
the high band, and the 8 kHz functions keep working in a v3.8 build.

**What the layer does.** The encoder takes 16 kHz speech, decimates it for the core
(scipy's `resample_poly` filter, 41 taps, run with its 20 samples of look-ahead so the two
sides see the same 8 kHz samples), and runs the v3.8 voicing rules over a ring of the
core's frames: onset recoding, the whisper rule, voicing smoothing, the burst rule and the
sibilant rule. All-unvoiced frames then get the 6-bit 4-8 kHz index in their pitch field
and one bit of the speaker-shape side channel. Both are measured against the encoder's
own decoder, a second core decoder inside the encoder, as the Python's `_low16` does. The
decoder upsamples the core's output and adds a 4-8 kHz noise band per 10 ms STFT frame:
consonant hiss from the index, a vowel-following layer from the decoded 0-4 kHz spectrum
and the speaker shape, blended per bin. The core itself changed in three places for v3.8:
unvoiced halves are synthesised 50 Hz apart whatever the pitch field says, upward-tilted
ones 3 dB louder, and a voiced second half between two unvoiced frames keeps its own pitch.

**Delay.** The Python model is not causal, and the port keeps its behaviour at the price
of look-ahead:

| Stage | v3.7 | v3.8 |
|---|---|---|
| Encoder: pitch tracker | 90 ms | 90 ms |
| Encoder: resampler | - | 10 ms |
| Encoder: voicing rules (`SOTTO_RULE_LAG` frames) and the index | - | 220 ms (lag 10) |
| Decoder: interpolation | 20 ms | 20 ms |
| Decoder: predictor on the decoded audio, frame lag, flicker bridging | - | 80 ms |
| **Codec in all** | **110 ms** | **420 ms** |

The 16 kHz encoder emits its first frame on the 17th call and the decoder's output is
5 frames late (`SOTTO_DEC16_DELAY_FRAMES`); the `-16` tools remove that delay. The rule
look-ahead is a build option: `make -C c RULE_LAG=5`. The whisper rule is what needs it: the
Python judges a run of mixed frames on the whole run, the C on its first `RULE_LAG + 1`
frames, and `SottoV38(whisper_lag=...)` makes the Python do the same so the two can be
compared. The other rules look at most 3 frames ahead and the index one frame.

**Checked against Python** (`tools/check_c_port38.py`, 7 s of five clips: three test voices
and the two whispered bench recordings, `docs/results/c_port38_lag*.json`). "Lag" is the
share of frames whose 48 bits match the Python with the same whisper look-ahead; "full" the
share against the Python as Jeff heard it (whole-run whisper judgement):

| `RULE_LAG` | Encoder delay | 2428 m | p020 m | 6313 f | whisper (Jeff) | whisper (wife) |
|---|---|---|---|---|---|---|
| 20 | 520 ms | 100 / 100 | 98.3 / 98.3 | 100 / 100 | 100 / 83.4 | 100 / 100 |
| **10 (default)** | **320 ms** | 100 / 100 | 98.3 / 98.3 | 100 / 100 | 100 / 100 | 100 / 100 |
| 5 | 220 ms | 92.9 / 92.9 | 97.4 / 97.4 | 100 / 100 | 89.1 / 89.1 | 90.0 / 90.0 |
| 3 | 180 ms | 93.1 / 89.1 | 96.6 / 96.6 | 100 / 100 | 82.9 / 77.7 | 87.9 / 87.9 |
| 1 | 140 ms | 96.9 / 86.3 | 96.3 / 96.3 | 97.7 / 97.7 | 73.1 / 73.1 | 85.4 / 72.9 |

- At lag 10 the encoder is bit-exact with its Python counterpart on all five clips, and on
  these clips also with the whole-run Python. The 1.7% on p020 is one shape near-tie (as in
  the v3.5 check) whose different decoded envelope moves the side-channel message by a
  step: 1 index and 4 side-channel bits.
- Below lag 10 the whisper rule no longer sees a whole 10-frame run before the first frame
  leaves, so whispered stretches start as mixed frames (the hum v3.8 set out to remove) and
  7 to 27% of their frames differ. PESQ barely moves (it does not hear the hum), so the
  choice is Jeff's ears against 100 ms of latency per 5 frames.
- Decoder: given the same bits, the 0-4 kHz band of the two outputs agrees to 32-41 dB SNR
  (the hiss near 4 kHz leaks into the measurement with its random phase; the 8 kHz core
  alone agrees to 100 dB). The 4-8 kHz level per 10 ms frame differs by 0.7 dB mean and
  1.6-1.9 dB at the 95th percentile, which is exactly the spread between two Python seeds
  (0.73 and 1.92 dB): the two decoders differ only in their random phases. PESQ of the C
  encoder and decoder together equals the Python's on 4 of 5 clips and is 0.04 lower on p020.

**Kept as the Python has it.** Two things in the Python look accidental but are part of
the sound Jeff approved, so the C keeps them: mixed frames' consonant hiss is scaled by
their mask value (3, so -3 dB + 9.5 dB), and the concealment fade is applied twice to the
consonant layer (through the decoded envelope and again explicitly).

**For the DSP.** The layer adds, per 10 ms, two 320-point real FFTs in the decoder (one on
the decoded audio for the predictor, one inverse for the hiss) and in the encoder one on
the input, one on the local decoder's output, plus the whole core decoder running inside
the encoder. The radio is half duplex, so the encoder's local decoder reuses the decoder's
cycles; the FFTs are a few MHz on the C5517's accelerator. The encoder's ring of 128 frames
and 64 STFT frames of features is about 40 KB as doubles and would shrink to 16-bit
fields. The running percentile of the side channel keeps every frame level so far (the
Python does); a 1 dB histogram would replace it on the DSP, changing a vowel-frame
decision only when a level sits within the bin.

## Fixed point: status

The fixed-point build is in `c/fixed/` (`make fixed`). It is new MIT code, written from the
float model's behaviour, not a conversion of Codec 2. It uses only integers: tables
generated by `tools/gen_fx_tables.py`, log2 and exp2 by table, levels in dB Q8, and
frequencies and phases as Q32 turns so phase accumulators wrap for free. It is built with
`-DFX_CHECK`, which stops the program if any sum of products would overflow the C55x's
40-bit accumulator. The rules that keep the x86 build and a C55x build the same are at the
top of `c/fixed/fx.h`.

**Decoder: done** (`c/fixed/sotto_fx_dec.c`, the same `sotto_dec_*` API).

- It follows the float decoder step by step. Harmonic levels agree to 0.01 dB and phases to
  0.002 rad. The minimum phase comes from two small cos/sin matrix products (65 × 63)
  instead of two 128-point FFTs. Only the 160 output samples the overlap-add needs are
  computed, as direct sums over the harmonics, instead of a 512-point inverse FFT.
- The outputs are not sample-identical over a whole clip. Where a harmonic sits on the
  postfilter's noise threshold, one decoder randomises its phase and the other doesn't, and
  from then on their random-phase sequences are out of step. That is no worse than the
  float decoder's own seed: changing only its seed moves ve9qrp's PESQ between 3.07
  and 3.18.
- Over 19 voices (`tools/check_fx.py`, `docs/results/fx_dec_check*.json`), with the same
  bits from the float encoder:

  | Channel | PESQ, float decoder | PESQ, fixed-point decoder |
  |---|---:|---:|
  | Clean | 2.924 | 2.925 |
  | 10% frame loss | 2.481 | 2.492 |

- Size on x86: 5 KB of code, 34 KB of tables (29 KB of them the codebook), and 0.8 KB of
  state.

**Encoder: done** (`c/fixed/sotto_fx_enc.c`, the same `sotto_enc_*` API).

- It follows the float encoder step by step: Codec 2's nlp pitch estimator in block floating
  point, the harmonic-sum refinement and MBE voicing fit, sotto's tracker with its costs in
  natural-log units (Q16), the rate-K envelope, and the M-best codebook search.
- The spectra come from a 512-point integer FFT. Instead of the float encoder's 16 stored
  spectra (70 KB), it keeps the last 1,520 input samples and redoes the FFT for the frame
  the tracker is deciding, 80 ms later.
- The codebook search, the largest cost, is written as one 16 × 16-bit multiply-add per
  value: |r − y|² = |r|² − 2 r·y + |y|², with |y|² from a table. That gives exactly the same
  distances as the direct form.
- Over 19 voices, 11,173 frames (`tools/check_fx.py --enc`, `docs/results/fx_enc_check.json`),
  with v3.7's pitch tracker. The C55x version (2026-11-02, below) rounds two steps more
  coarsely so they fit 40-bit arithmetic: the least-squares amplitudes in the voicing fit
  (one 32-bit division, about 15 significant bits) and the bin powers for the pitch searches
  (from bins rounded to 19 bits). That moved a few near-ties:

  | | Before (PR #8) | Now |
  |---|---:|---:|
  | Frames whose 48 bits match the float encoder | 97.8% | 97.5% |
  | Frames differing in voicing / pitch / energy / shape | 34 / 103 / 30 / 203 | 34 / 104 / 30 / 243 |
  | PESQ, float encoder + float decoder | 2.947 | 2.947 |
  | PESQ, fixed encoder + fixed decoder | 2.944 | 2.945 |
  | PESQ, fixed encoder + float decoder | 2.934 | 2.944 |

  The frames that differ are near-ties: a pitch or band edge exactly on a grid point, or two
  codebook paths at almost the same distance, where float rounding decides. Integer code
  can't copy float rounding, and these choices are equally good.
- All stress signals (silence, full-scale noise and square waves, clicks, a chirp, speech,
  and random bits with losses for the decoder) run clean under AddressSanitizer,
  UndefinedBehaviorSanitizer and `FX_CHECK`.
- Size on x86: 13 KB of code and 15.7 KB of state (3 KB of input history, two 2 KB spectra,
  4 KB of FFT scratch). The tables are shared with the decoder, 43 KB in all with the 6 KB
  of codebook lengths.

**v3.8 layer: done** (`c/fixed/sotto_fx_hiss.c`, the same `sotto_enc16_*` / `sotto_dec16_*`
API as the float layer, `make fixed` with the default `VARIANT=v3.8`; `-16` on the tools).
*2026-11-02.*

- It follows `c/sotto_hiss.c` call for call: the same rings, the same look-ahead
  (`SOTTO_RULE_LAG`, 320 ms in the encoder, 100 ms in the decoder), the same rules, side
  channel and blend. The core changes of v3.8 are in the fixed core too: unvoiced halves at
  50 Hz spacing, +3 dB for upward-tilted ones, the voiced-second-half guard, the envelope at
  the hiss band's reference frequencies and the per-half rule features (`fx_internal.h`).
- Integers: the 320-point STFT is a 5 × 64 FFT (five 64-point `fx_fft`s and radix-5
  butterflies with Q15 twiddles) in block floating point, each frame scaled to 22 bits and
  its bin powers carrying one exponent; band levels are dB Q8 from `fx_log2`, the
  regression weights Q14, the pitch logs Q16, the two 30 ms one-poles Q15, the resamplers
  the same 41 half-band taps in Q19, and the running 60th percentile is exact (an integer
  comparison instead of the interpolated value). The noise band is synthesised by the same
  FFT on the conjugate spectrum, with the float layer's phase sequence (xorshift32 read as
  Q32 turns), so the two hiss bands agree sample for sample within rounding.
- Checked against the float port on the same five 7 s clips as the float-vs-Python check
  (`tools/check_fx.py --v38`, `docs/results/fx38_check.json`):

  | | 2428 m | p020 m | 6313 f | whisper (Jeff) | whisper (wife) | all |
  |---|---:|---:|---:|---:|---:|---:|
  | Frames whose 48 bits match | 95.1% | 91.1% | 97.4% | 94.6% | 97.5% | 95.1% |
  | Differing in voicing / index / side bit / energy / shape | 4/6/10/0/5 | 0/9/5/4/28 | 0/3/3/1/4 | 0/5/9/1/7 | 0/2/3/0/5 | 4/25/30/6/49 |
  | 4-8 kHz level, mean / 95th pct. difference per 10 ms (dB) | 0.38 / 1.82 | 0.35 / 1.53 | 0.57 / 1.91 | 0.12 / 0.37 | 0.13 / 0.38 | 0.31 / 1.20 |
  | PESQ, float encoder + float decoder | 3.208 | 3.028 | 2.536 | 2.258 | 2.529 | 2.712 |
  | PESQ, float encoder + fixed decoder | 3.196 | 3.092 | 2.551 | 2.340 | 2.384 | 2.713 |
  | PESQ, fixed encoder + fixed decoder | 3.261 | 3.017 | 2.571 | 2.330 | 2.385 | 2.713 |

  The frames that differ are near-ties again, now in more places: the 8 kHz core sees the
  fixed resampler's output (Q19 taps, rounded to 16 bits, so a few samples differ by one
  LSB from the float's double-precision ones), the hiss index is a 64-way nearest-vector
  choice on four band levels, and a speaker-shape coefficient on a 4 dB step boundary
  changes the whole 16-bit message of its window (the "side" column). The same clips at
  8 kHz give 96.9% matching frames with this build, against the 97.5% of the 19-voice check.
  The FFT, band levels and resamplers were also checked one primitive at a time against
  numpy: FFT bins to 4 × 10⁻⁵ relative, band levels to 0.005 dB, the decimator exact and the
  interpolator within one LSB.
- The two decoders' 0-4 kHz outputs agree in level, not in samples. Their harmonic levels
  match to 0.02 dB on average, but with 80 random-phase harmonics in every unvoiced half the
  background-noise postfilter's threshold decisions put the two phase generators out of step
  within the first frames of a quiet start, so the sample SNR between them is 2 to 20 dB
  where it was 10 to 65 dB for v3.7. Compared by level (500 Hz bands over 80 ms windows) the
  two differ by 0.3 to 0.6 dB median and 0 to 0.14 dB in total energy; `tests/test_vocoder.py`
  now checks that instead of the sample SNR.
- Stress signals (silence, full-scale noise and square waves, clicks, a chirp, a 1 ms
  input, an input that is not a multiple of 320, speech, random bits with 30-50% losses and
  with every frame after the first lost) run clean under AddressSanitizer,
  UndefinedBehaviorSanitizer and `FX_CHECK`.
- Size on x86: 19 KB of code and 2.6 KB more tables (the hiss weights, codebook, resampler
  taps and the 320-point window and twiddles). State: 23.5 KB for the 16 kHz decoder (its
  core, 2 KB of overlap-add in 32 bits, a 4 KB 16 kHz ring, 5 KB of FFT work) and 78 KB for
  the encoder, 48 KB of which is the running percentile's sorted history (`FX_PCT_MAX`,
  12,000 frames = 120 s, as the Python keeps everything; 3,000 frames would do on the DSP
  and saves 36 KB). All ring sizes are checked at run time under `FX_CHECK`.
- Measured on the C55x simulator: see "v3.8 on the C55x" below.

## Fixed point on the C55x: measured

*2026-11-02.* Built with TI's C5500 compiler 4.4.1 (`-O3`, large memory model, the C5517's
CPU revision 3.3) and run on TI's cycle-accurate C55x Rev 3.0 CPU simulator. The program is
`c/fixed/c55x/bench.c`. It counts the cycles of every `sotto_encode` and `sotto_decode` call,
and `c/fixed/c55x/README.md` explains how to set up the tools and run it. The input is 10 s
of ve9qrp (500 frames). The DSP's bits and audio are identical to the x86 build's.

**Cycles.** These are MHz of the C5517's 200 MHz. "Mean" is the average over the 500 frames,
and "worst frame" is the most expensive single frame, which is what has to fit in real time.

| | Estimate (2026-11-01) | First C55x build | Now: mean | Now: worst frame |
|---|---:|---:|---:|---:|
| Encoder | 25–50 | 802 | **188** | **212** |
| Decoder | 4–8 | 106 | **36.7** | **43.9** |
| Encoder, codebook unpacked (`-DFX_CB_UNPACKED`, 1 s) | | | 169 | 192 |

The radio is half duplex, so only one of the two runs at a time. The encoder doesn't fit
yet: at its worst frame it needs more than the whole DSP, before the modem and FEC.

**Memory.** These are bytes; one C55x word is 2 bytes.

| | Estimate (x86 sizes) | C55x |
|---|---:|---:|
| Encoder state | 15.7 KB | 16.2 KB |
| Decoder state | 0.8 KB | 0.8 KB |
| Tables | 43 KB | 45.7 KB (76.4 KB with the codebook unpacked) |
| Code | 18 KB | 17.4 KB (encoder 11.9, decoder 3.6, arithmetic 1.9), plus a few KB of TI's run-time library |
| Stack | not estimated | 6.0 KB at most (first reported as 14.4 KB, see below) |
| **Total** | **about 78 KB** | **about 90 KB of the C5517's 320 KB** |

The RAM estimate held. The tables grew because the C55x has no 8-bit type: the codebook is
stored two values to a 16-bit word, padded to 20 values a vector, and the other small tables
take a word a value. Most of the stack is the encoder's copy of a whole spectrum (2 KB) and
the codebook search's 8 paths (2.4 KB). The first version of this page gave 14.4 KB: the
benchmark read the linker's stack symbols, which are byte addresses, as word addresses, and
so counted 8 KB of other memory as stack (fixed 2026-11-02).

**Where the encoder's time goes** (1 s, MHz averaged over frames, from the profiling build,
whose timer calls add about 7% to the total):

| Block | MHz | What it does |
|---|---:|---|
| Codebook search | 48 | 8,704 vectors a frame (8 paths), 10 packed words each |
| 512-point FFT | 35 | 4 a frame: one per 10 ms analysis, and one redone for each frame the tracker decides |
| Pitch refinement (`refine`) | 36 | harmonic sums at 20 trial pitches for each pitch candidate |
| Fit error (`ln_fit_error`) | 32 | the voicing fit for each candidate: 2 divisions per harmonic |
| nlp pitch estimator | 18 | 64-point DFT at 115 bins, and the 48-tap filter |
| Everything else in the spectra (window, scaling, bin powers) | 15 | |
| Everything else | about 17 | |
| **Decoder:** minimum phase / overlap-add sums / the rest | 16 / 10 / 10 | |

**Why the estimate was 4 to 5 times too low.** It assumed one or two cycles per inner-loop
step, which is what hand-written C55x assembly gets. Compiled C gets much less:

- A 32 × 16-bit product needs two 16 × 16 multiplies, and any sum wider than 32 bits makes
  the compiler switch the accumulator mode inside the loop. One FFT butterfly takes about
  77 cycles (estimate 10 to 16), so one FFT takes 177,000 cycles.
- The FFT runs 4 times a frame, not 2.
- Division is a bit-serial library call of 100 cycles or more. The voicing fit does two per
  harmonic, for every candidate.
- Every `long long` product of two 32-bit values is a library call. The first build was full
  of them, which is why it needed 802 MHz.

**What changed for the C55x** (all in `c/fixed/`, the x86 build gives the same bits):

- 40-bit `long long`: `fx_acc` is the accumulator type, the 2^48 reciprocal and the other
  wide divisions use `fx_udiv_shl` (exact, in 40-bit steps), and `fx_log2_acc` takes a 40-bit
  value.
- 16-bit `int` and `char`, C89 declarations, and the packed codebook (`tools/gen_fx_tables.py`).
- Speed, keeping the bits: 32 × 32 products split into 16 × 16 ones; the reciprocal of a
  pitch period with one division and a correction step (checked against 64-bit division for
  every input from 2^20 to 2^28); bin positions stepped instead of multiplied; the codebook
  search on packed words without unpacking; the FFT butterfly in 32-bit arithmetic only; and
  the codebook search skipping a vector as soon as a 32-bit test shows it can't make the list.

**v3.8 on the C55x** (2026-11-02). `-DBENCH16` runs 16 kHz audio through the v3.8 layer
(`sotto_encode16` / `sotto_decode16`). The input is LibriSpeech 198-209-0000 (the female
clip, 13.9 s, resampled to 16 kHz), since ve9qrp exists only at 8 kHz. The DSP's bits match
the x86 build's and its audio matches the x86 decoder's sample for sample (5 frames later:
`sotto_dec -16` removes the layer's delay, `bench.c` doesn't).

| | v3.7 (8 kHz, ve9qrp) | v3.8: mean | v3.8: worst frame | Estimate for the layer |
|---|---:|---:|---:|---:|
| Encoder | 188 / 212 | **288** | **328** | core + 45–50 |
| Decoder | 36.7 / 43.9 | **92.3** | **106.8** | core + 5–10 |

Where the layer's time goes (2 s, profiling build, MHz):

| Encoder | MHz | Decoder | MHz |
|---|---:|---|---:|
| 8 kHz core encoder | 204 | 8 kHz core decoder (80 harmonics in unvoiced halves) | 41 |
| 2:1 decimator (41 taps) | 7 | 1:2 interpolator | 10 |
| input STFT and band levels | 16 | stage A: STFT of the decoded audio, predictor | 17 |
| local core decoder (the encoder's own copy) | 41 | stage B: hiss synthesis (inverse FFT, overlap-add) | 24 |
| local 1:2 interpolator and STFT | 27 | | |
| rules, index, side bit | 4 | | |

The layer adds about 80 MHz to the encoder and 50 MHz to the decoder, far more than the
estimate. The causes are the same as for the core: a 320-point STFT costs over 100,000
cycles in compiled C, and each side runs four per 20 ms; the 41-tap resamplers multiply
32 × 16 bits. The encoder also decodes its own frames, which costs as much as the
decoder's core. (The 8 kHz core itself got about 7 MHz slower with v3.8's core changes:
194 MHz on the 1 s ve9qrp run with `C55X_VARIANT=v3.7`, against 187 at PR #11.)

Fixes in this round (all exact, the same bits and samples on x86 before and after):

- `sotto_fx_dec.c`: the hiss band's reference frequencies were written as `3800 * 16`,
  which overflows the C55x's 16-bit `int` (60,800), so the DSP's table was wrong. They are
  `long` constants now. The x86 build was unaffected.
- The radix-5 stage of the 320-point FFT in 32-bit arithmetic (`fx_q15_mac2`): about 10 MHz
  less on each of the four STFT paths.
- The resamplers gather their input without bounds tests except at the start of the
  stream: 11 MHz less in the encoder, 4 in the decoder.
- The running percentile finds its rank by binary search. The linear scan grew with the
  talk time: by our estimate, about 10 MHz after two minutes.
- `bench16.cmd`: v3.8's states (about 120 KB with both directions) don't fit the 64 KB of
  DARAM, so the heap gets a 64K-word page of SARAM. The heap size is in bytes, which
  `c55x_build.sh` had as words.

**Memory with v3.8** (both directions allocated, as in `bench.c`):

| | Size |
|---|---:|
| 16 kHz encoder state (its core encoder 16.2 KB and local decoder 0.8 KB extra) | 80.1 KB (+17 KB) |
| 16 kHz decoder state (its core decoder 0.8 KB extra) | 23.5 KB (+0.8 KB) |
| Tables | 49.4 KB |
| Code (layer 13.5 KB, encoder 12.3, decoder 4.0, arithmetic 1.9) | 31.7 KB |
| Stack | 6.9 KB at most |
| **Total** | **about 210 KB of 320 KB** |

`-DFX_PCT_MAX=3000` (30 s of percentile history instead of 120 s) saves 36 KB.

**Next, to fit with room for the modem.** In order of MHz saved for the work:

1. **FFT in assembly**, or on the C5517's FFT accelerator (16-bit data, which changes the low
   bits and needs a check against float): 35 MHz to under 10.
2. **Codebook search:** unpack the codebook (29 KB more, saves about 20 MHz at the worst
   frame), or write the search with the C55x's dual multiply-accumulate (2 vectors at once).
3. **Voicing fit:** one reciprocal per harmonic instead of two divisions, and the fit error
   from the sums instead of a second pass. That changes rounding, so it needs the 19-voice
   check again. About 15 to 20 MHz.
4. **Keep the decided frame's spectrum** instead of redoing its FFT: saves a quarter of the
   FFT time for 18 KB more state.
5. **Decoder:** the overlap-add as one 256-point inverse FFT, and the minimum phase with
   FFTs, as Codec 2 does. About 15 MHz.

With 1 to 3, the encoder should land near 100 MHz at its worst frame, and the decoder near
25 MHz. A C5517 board is still needed to confirm the simulator against real memory wait
states.

## Fixed point: the plan

The C55x has no floating-point unit. Software float would be far too slow for the analysis
and synthesis, so the DSP build needs a fixed-point version of every block, not only
sotto's. In order:

1. **Codec 2's analysis and synthesis in fixed point.**
   - FFTs on the C5517's hardware FFT accelerator, with block floating point for the spectra.
   - nlp, harmonic-sum refinement, amplitudes and the MBE voicing fit.
   - Zero-phase synthesis (128-point FFT, log and exp tables).
   - Postfilter and overlap-add.

   Codec 2 has no official fixed-point version, so this is the largest piece. A fixed-point
   reference that compiles on x86 comes first, then the C5517 build. The x86 reference uses
   ETSI-style basic operations, so the same C runs on both.
2. **sotto's own blocks.**
   - The tracker's costs as base-2 logs in Q8.
   - The rate-K envelope in Q8 dB.
   - 8-bit codebooks with a 32-bit accumulator search.
   - Interpolation and set-energy with a table exp.
3. **Encoder memory.** The float encoder keeps 16 frames of 512-point spectra (70 KB) so the
   tracker can revisit them after its 80 ms delay. The DSP version should keep the last
   1,200 input samples (2.4 KB) instead, and redo the FFT for the frame being decided. The
   FFT accelerator makes that cheap.
4. **Check against the float port** with `tools/check_c_port.py` (PESQ within 0.05 and the
   same bits on most frames). Then measure cycles with TI's free C55x compiler and its
   simulator, or on a C5517 evaluation board.

None of this needs the radio. Running it on the radio still depends on the DSP update path
(workstream 3), which is open.
