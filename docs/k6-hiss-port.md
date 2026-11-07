# Sizing the hiss band for the K6 (2026-11-07)

The speaker sweep showed that the K6 speaker passes 4-8 kHz ([k6-receiver.md](k6-receiver.md), "The
audio path"), so the 4-8 kHz hiss band of sotto 600 / 400 / 1200 is worth playing on the K6.
This page sizes that port before any firmware change: CPU time, RAM, and the 16 kHz output.

**In short:**

- The v3.8 hiss layer as it is doesn't fit. It adds about 65 MHz and 23.5 KB of RAM to a decoder
  that already uses 58-77 MHz of the K6's 96 MHz, and the pager has well under 1 KB of RAM free.
- A lighter K6 layer makes the same sound by cheaper means. In Python it scores within 0.01
  PESQ and 0.3 dB of band error of the full layer. In fixed point it adds 22.7 MHz on the M0
  model, but needs 4.8 KB of RAM once trimmed: the whole 4 KB message store.
- Jeff ruled out writing the SPI flash at run time (2026-11-07), so the store stays in RAM. Tables
  in flash, read as ROM, are fine.
- **The hiss made with the core's output** (below, `-DFX_HISS_FB`) removes the RAM problem's bulk.
  There is no inverse FFT and no audio held back: four band gains per 10 ms frame drive four
  order-3 filters at 8 kHz on one noise, in step with each core block. It scores like the light
  layer, and its state is 1.0 KB instead of 12.9 KB. With two cheaper options Jeff couldn't hear
  (half the filters, gains every 20 ms) it adds 12.3 MHz on the model, 9.5 MHz with the code in
  zero-wait RAM.
- What's left: about 1 KB of RAM more than the pager has free (state, stack and a 16 kHz PWM ring),
  and the CPU, which fits only with the hot loops in RAM. Next steps are at the end.
- One exact speed-up came out of the measuring: the resamplers now skip their zero taps.

## What the v3.8 layer costs on a Cortex-M0

Measured on the Cortex-M0 emulator (`tools/cm0_build.sh`, `c/fixed/cm0/run.py`, -O2, two flash
wait states, the timing model's ±25%). The input is 6 s of LibriSpeech 3536 (female), 300 frames
of sotto 2400 from the float v3.8 encoder. Decoder only:

| | Mean MHz | Worst frame MHz | Decoder state |
|---|---:|---:|---:|
| v3.7 decoder (8 kHz, what the K6 plays) | 47.0 | 74.9 | 0.8 KB |
| v3.8 decoder (16 kHz, the hiss layer), before | 116.6 | 149.4 | 24.4 KB |
| the same, code in zero-wait RAM | 95.9 | 122.6 | |
| v3.8 decoder, after the resampler change below | **110.2** | **143.1** | 24.4 KB |

Where the extra 63 MHz goes (MHz, flash, after the resampler change):

| Part | MHz |
|---|---:|
| The four 320-point FFTs per 20 ms (`fx_fft`, `fft5x`, butterflies, split and pre-pass) | 22.7 |
| The v3.8 core's unvoiced halves (80 harmonics at 50 Hz spacing, `fx_oa_block` +8.4, `synth` +0.7) | 9.1 |
| Stage B: per-bin levels, random phase, overlap-add (`dec_run`) | 8.2 |
| 64-bit helpers (`__aeabi_lmul`, `__aeabi_lasr`) | 5.6 |
| Stage A's band powers and the vowel predictor (`fx_bins_pow`, `stft_power`, `fine_predict`, ...) | 5.0 |
| 1:2 interpolator (`up2`, was 10.3) | 4.1 |
| The rest (`fx_cos`, `fx_exp2`, dB conversions, the call itself) | 8.5 |

The budget it has to fit:

- **The pager's 600 / 400 decode plus PWM**, measured on the same model (`c/k6/bench_k6.py`): 57.7
  MHz mean and 76.6 MHz worst for sotto 400.
- **On the radio it is slower than the model.** The decoder's own timer read 15.5 ms mean and 21.2
  ms worst per 20 ms frame. That is about 74 MHz mean, 1.29 times the model, which matches the
  1.33 times that code in flash costs against code in RAM (`docs/k6-modules.md`, Insight 29).
- **The worst frame is already over 20 ms.** The 80 ms PWM ring absorbs it. There is no headroom
  for 65 MHz more.

**RAM** is tighter still:

- The layer's state is 23.5 KB on top of the core. Most of it is rings sized for a DSP: 4 KB of
  16 kHz audio, 8 KB of 32-bit overlap-add, 4 KB of envelopes, and 5 KB of FFT work.
- The pager uses about 12.3 KB of Aura's 17.7 KB. Only about 660 bytes are free while 600 / 400
  play.
- 16 kHz output doubles the PWM ring from 1,280 to 2,560 bytes by itself.

## A lighter layer for the K6

These steps keep what Jeff chose: consonant hiss from the coded index, the vowel-following layer,
the speaker shape, and the blend. They change how the layer is computed.

| Step | Saves | Exact? |
|---|---:|---|
| 1. Half-band resamplers skip their zero taps (done, below) | 6.4 MHz | yes |
| 2. The vowel predictor reads the decoded envelope the core already has, not an STFT of the decoded audio | the 2 analysis FFTs and stage A's band powers, about 16 MHz; the 16 kHz audio ring | no: needs a score and a listen |
| 3. The hiss is made at 8 kHz as its mirror image and folded in through the interpolator: out = up2(y8 − s) + 2 · s zero-stuffed | the 320-point inverse FFT becomes a 160-point one, about 6 MHz; the overlap-add halves | the same sound, not the same samples |
| 4. Shape only the bins up to about 7 kHz | about 3 MHz | no: on the K6, the PWM image of a 7.5 kHz tone (at 8.5 kHz) is 2 dB louder than the tone itself |
| 5. Keep the v3.7 core's unvoiced halves, as the K6 decoder has now | 9 MHz | no: whether the hiss decoders need v3.8's 80-harmonic halves isn't measured yet |
| 6. Copy the hot loops (core synthesis, FFT) into RAM (Insight 29) | about a quarter of their time on the radio | yes, but it costs RAM for the code |

Step 3 works because the interpolator is a half-band filter. Its complement passes 4-8 kHz, so
2 · stuff(s) − up2(s) is the mirror of s about 4 kHz. Adding it to up2(y8) costs one
interpolator instead of two.

**Estimate for the light layer, and what the fixed-point build measured** (see "The light layer
in fixed point" below):

- **CPU:** the estimate was 15-25 MHz on the model. Measured: 22.7 MHz mean (69.7 against the
  plain decoder's 47.0), 97.4 MHz in the worst frame, and 55.4 / 77.8 MHz with the code in
  zero-wait RAM.
- **On the radio:** it ran 1.28 times the model for sotto 400 in flash. Played from flash, 600 / 400
  plus the light layer would need about 100 MHz mean, so they don't fit. With the hot loops in RAM
  they might fit on average, but the worst frames would still lean on the PWM ring.
- **RAM:** the build has 12.9 KB of layer state with generous rings. Trimmed for the K6, the
  minimum is about 3.5 KB:
  - about 60 ms of 8 kHz audio waiting for its hiss, 1 KB;
  - the 8 kHz overlap-add in 16 bits, 1.3 KB;
  - per-frame levels in 16 bits, 1 KB.

  The FFT scratch (2.5 KB) can share the decoder's stack depth, because the layer runs after the
  core returns. With the PWM ring growing by 1.3 KB for 16 kHz, that is about 4.8 KB, against
  0.66 KB free, and that's before any code copied into RAM.

**Where the RAM would come from** (superseded: Jeff ruled out run-time SPI writes, and the layer
made with the core's output, below, needs about 1 KB). The message store is the only big buffer,
and the light layer needs about all of it:

- It is 4 KB today. That holds 12.8 s of 2400, 27 s of 1200, 55 s of 600 or 82 s of 400.
- Giving it up for the hiss leaves almost no store. So a hiss image would need the store moved
  to the SPI flash. The SPI flash already holds the codebooks and the repeater table, and the
  packets arrive at about 170 bytes/s.
  - A 4 KB sector erase takes tens of ms and would have to fall between packets. That timing is
    not measured.
  - The flash also wears with each message, a sector or two per message, against an endurance
    of about 100,000 cycles.
- The other knob, `-DFX_OA_UNPACKED`, frees 2 KB but costs about 10 MHz, which the layer can't
  spare.

**Image or module.** It needs an image change, so one reflash, and here is why:

- The 16 kHz PWM rate and a per-frame hook in the playback path live in the image.
- The layer itself is too big for the 4 KB module store.
- Per the RAM-module rule, test pieces (the 16 kHz PWM check, a tone through the folded
  interpolator) can still go as modules first.

## Proposed steps

1. **Done: Python prototype of the light layer** (steps 2-5; see below). It scores within 0.01 of the full layer. The plan was to measure, on the 16 held-out voices:
   - PESQ-WB and the fricative hiss error against the full v3.8 layer;
   - a blind pair at the clips' fricative spots.

   No firmware.
2. **Done: the fixed-point light layer and the M0 bench** (see below). It adds 22.7 MHz, and needs
   about 4.8 KB of RAM once trimmed.
3. **Done: the store stays in RAM.** Jeff ruled out SPI writes at run time (2026-11-07). The layer
   is now made with the core's output (below), which needs about 1 KB, not 4.8 KB.
4. **One image** with 16 kHz PWM, the hook and the layer. First a RAM module to check the 16 kHz
   PWM path on air.

## The light layer in Python (2026-11-07)

Steps 2-5 are decoder options of `SottoV38` in `sim/hiss_band.py`:

- `vowel_src="model"`: the vowel predictor reads the decoded model (`model_bands`,
  `fine_levels_model`). The harmonic amplitudes become a power density on 50 Hz bins, and the
  same ridge weights are applied. `MODEL_REF_DB` (−27.6 dB) puts that density on the STFT's
  scale. It was measured on four Codec 2 clips outside the test voices.
- `synth8=True`: the hiss is made at 8 kHz and folded in (`fold8`).
- `uv50=False`: v3.7's unvoiced halves.
- `hb_max_hz`: no hiss above that frequency.

The encoder and the bits are unchanged.

**The model and the STFT agree.** On three test voices, the predictor's 16 vowel-layer levels from
the model are within 1.9-2.9 dB rms of those from the STFT, frame by frame. The model's mean is
0.7-1.4 dB lower. Model frame t + 2 lines up best with STFT frame t, as `HB_LAG` says. The folded
synthesis matches the 320-point one's spectrum within 0.5 dB across 4.5-7.9 kHz.

**Scores.** `tools/eval_k6_hiss.py`, the 16 held-out voices, each encoded once by the hiss encoder,
then decoded by each variant (`docs/results/k6_hiss_light.json`). The band errors are 4-8 kHz level
errors per 10 ms against the original. The fricative error is the one `tools/eval_lr_hiss.py`
reports. The vowel error is the same on frames whose 0-4 kHz band is at least 20 dB over 4-8 kHz.

| | PESQ-NB | PESQ-WB | Fricative error, rms / bias dB | Vowel error, rms / bias dB |
|---|---:|---:|---:|---:|
| **sotto 600**, full v3.8 layer | 2.113 | 1.354 | 8.56 / −3.43 | 14.59 / −8.73 |
| light (model, folded, v3.7 halves) | 2.104 | 1.345 | 8.41 / −3.17 | 14.45 / −8.50 |
| light, no hiss above 7 kHz | 2.104 | 1.358 | 11.78 / −6.26 | 15.97 / −10.60 |
| plain decoder (what the K6 plays now) | 2.113 | 1.399 | 64.66 / −59.70 | 31.64 / −28.26 |
| **sotto 400**, full | 1.854 | 1.256 | 11.70 / −6.27 | 14.53 / −8.55 |
| light | 1.852 | 1.255 | 11.53 / −5.95 | 14.38 / −8.27 |
| **sotto 1200**, full | 2.544 | 1.555 | 5.25 / −1.14 | 13.39 / −7.80 |
| light | 2.533 | 1.550 | 5.28 / −0.70 | 13.25 / −7.58 |

Each step taken alone moves the scores by 0.01 or less, and the band errors by 0.3 dB or less
(see the json). So the light layer sounds like the full one by these measures, at about a third
of the CPU.

Two notes on the table:

- The 7 kHz cap costs about 3 dB of band error, because that measure counts the missing 7-8 kHz
  band. On the K6 those frequencies are 11-14 dB down in the speaker anyway, and the PWM image
  would land above them.
- PESQ-WB scores the plain decoder higher than any hiss layer, as it did for sotto 1200 hiss.
  Listening rounds 19 and 21 went the other way, so PESQ-WB isn't the judge here.

## The light layer in fixed point (2026-11-07)

`-DFX_HISS_LIGHT` builds the 16 kHz decoder (`c/fixed/sotto_fx_hiss.c`) as the light layer. It is
a decoder-only build: its local decoder changes the encoder's bits too, so the encoder isn't for
use.

- **The core** (`c/fixed/sotto_fx_dec.c`) keeps v3.7's unvoiced halves and puts `model_bands`
  into `fx_dec_info_t.bands`. That is 16 band levels per half from the harmonic levels it already
  has: 80 interpolations and 16 power sums, with one division per half.
- **Stage A** applies the vowel predictor's ridge weights to those bands, with no STFT.
- **Stage B** makes the hiss as a 160-point inverse FFT (5 × 16, the existing radix-5 code) at
  8 kHz, mirrored.
- **The output** is `up2_q15(y8 − s)` + 2 s zero-stuffed. `up2_q15` is the interpolator with the
  taps rounded to Q15, so its sums stay in 32 bits. It isn't exact with anything, and it doesn't
  need to be.

**Checks:**

- On LibriSpeech 3536 and EARS p020, its fricative and vowel band errors are within 0.2 dB of the
  Python light layer's on the same bits. Its 4-8 kHz level is within 0.1 dB.
- Random bits, losses and a one-frame stream run clean under AddressSanitizer,
  UndefinedBehaviorSanitizer and `FX_CHECK`.
- The default build is unchanged: the same bits and samples as before.

**Cortex-M0 model** (300 frames of LibriSpeech 3536, decoder only):

| | Mean MHz | Worst frame MHz | Mean, code in RAM | State |
|---|---:|---:|---:|---:|
| v3.7 decoder (plain, what the K6 plays) | 47.0 | 74.9 | | 0.8 KB |
| v3.8 16 kHz decoder (full layer) | 110.2 | 143.1 | | 24.4 KB |
| **light layer** | **69.7** | **97.4** | **55.4** | 13.7 KB |

The light layer's 22.7 MHz:

| Part | MHz |
|---|---:|
| Stage B: per-bin levels, phases, overlap-add (`dec_run`) | 4.4 |
| 64-bit helpers (`__aeabi_lmul`, `__aeabi_lasr`), mostly stage A's ridge and stage B | 4.2 |
| The band levels in the core (`fx_db_power_sum`, division) | 3.4 |
| The 160-point FFT (`fx_fft`, `fft5x`, butterflies, pre-pass) | 5.2 |
| The output fold (`dec_output` with `up2_q15`) | 2.9 |
| The rest (`fx_cos`, `fx_exp2`, dB conversions) | 2.6 |

Two cuts were taken on the way: the output's y8 − s is formed once per block, and the band levels
use one division per half, not two per bin. Together they took the layer from 45.7 to 25.3 MHz.

## The resampler change (exact)

The 41 taps of the 1:2 and 2:1 resamplers are a half-band filter: every even tap is 0 except the
centre one. In `c/fixed/sotto_fx_hiss.c`:

- **`up2`'s even output** is now one product, and its odd output is 20 taps, not 21.
- **`down2`** multiplies only the odd taps and the centre.
- **Off the M4 and C55x paths,** both add each symmetric pair of samples before multiplying. That
  is exact: a 15-bit tap half times a 17-bit sum fits in 32 bits.

The M4 path (`FX_ARM_DSP`) keeps its paired MACs for `down2`. The C55x path skips the zeros but
not the symmetry, and it is not run here (no TI compiler in this container).

Checked the same, byte for byte, as before the change: the fixed encoder's bits and the fixed
decoder's 16 kHz output on 4 voices, a full-scale square wave and full-scale noise, and the decoder
on random bits. Both the x86 and the M4 (`FX_ARM_DSP`) builds match.

| Cortex-M0 model, -O2 | Before | After |
|---|---:|---:|
| 16 kHz decoder, mean MHz | 116.6 | 110.2 |
| 16 kHz decoder, worst frame | 149.4 | 143.1 |
| 16 kHz encoder, mean MHz (150 frames) | 1187.4 | 1173.0 |

## The hiss made with the core's output (2026-11-07)

Jeff's direction: synthesize the hiss layer at the same time as the rest of the waveform. The
light layer's RAM went to its pipeline: 60 ms of 8 kHz audio waiting for its hiss, an 8 kHz
overlap-add, rings of levels between the stages, and FFT scratch. None of it is needed if each
core block gets its hiss as it comes out.

**What changes** (`sim/hiss_band.py` options `fb=4, fb_order=3, causal="none", synth8=True`;
fixed point `-DFX_HISS_LIGHT -DFX_HISS_FB` in `c/fixed/sotto_fx_hiss.c`):

- **No inverse FFT.** Stage B still works out the 4-8 kHz level of every 50 Hz bin (consonant and
  vowel layers, the louder per bin). It then sums them in power over four 1 kHz bands, so each
  10 ms frame gives four gains (`fb_gains`: 2 / N² Σ|X|², the inverse STFT's power per sample).
- **Four filters on one noise.** A white noise goes through four order-3 Butterworth filters at
  8 kHz, one per band, mirrored about 4 kHz: 4-5 kHz is a 3 kHz high pass, 7-8 kHz a 1 kHz low
  pass, and 5-6 and 6-7 kHz are band passes. Each output is scaled to unit power in its band, so
  the gains need no other correction. The gains are linear between frame centres, which fall on
  8 kHz sample 80 t + 80. The fold is unchanged: out = up2(y8 − s) + 2 s zero-stuffed.
- **In step with the core.** STFT frame t reads model frame t + 2 (`HB_LAG`), and that is the half
  the core has just made. So after core call n, the frames whose centres reach the end of its
  block are all computable. Each block gets its hiss at once. The decoder keeps the interpolator's
  10 samples and nothing more, so the 16 kHz output is 2 frames and 20 samples behind, not 5
  frames (`SOTTO_DEC16_DELAY_FRAMES` is 2 in this build).
- **No look at later frames' audio.** Blend's flicker bridge (a vowel layer kept on through an
  unvoiced gap of up to 4 frames, if the vowel comes back) needed to see 4 frames ahead. It is off
  (`causal="none"`), and it made no difference to the scores (600 fricative 8.56 dB either way).
  The codes of the next codec frame are still read, as the light layer does. Those are bits, not
  audio, so reading ahead costs a few bytes. Dropping that too (`causal="strict"`) costs about
  0.35 dB of fricative error, so it stays.

**Scores** (`tools/eval_k6_hiss.py`, 16 held-out voices, `docs/results/k6_hiss_light.json`;
fricative and vowel 4-8 kHz band error, dB rms):

| | 600 PESQ-NB | 600 fricative / vowel | 400 fricative / vowel | 1200 PESQ-NB | 1200 fricative / vowel |
|---|---:|---:|---:|---:|---:|
| full v3.8 layer | 2.113 | 8.56 / 14.59 | 11.70 / 14.53 | 2.544 | 5.25 / 13.39 |
| light layer | 2.104 | 8.41 / 14.45 | 11.53 / 14.38 | 2.533 | 5.28 / 13.25 |
| **made with the core's output** | 2.105 | 8.65 / 14.57 | 11.76 / 14.52 | 2.540 | 5.54 / 13.31 |
| + half the filters (`pair`) | 2.105 | 8.57 / 14.53 | 11.70 / 14.48 | 2.540 | 5.51 / 13.28 |
| + gains every 20 ms (`hop2`) | 2.105 | 8.99 / 14.29 | 11.90 / 14.26 | 2.539 | 5.84 / 13.02 |
| **both (`k6`): the C build** | 2.105 | 8.89 / 14.25 | 11.82 / 14.22 | 2.539 | 5.79 / 12.98 |
| plain (what the K6 plays) | 2.113 | 64.66 / 31.64 | 64.88 / 31.74 | 2.539 | 64.57 / 31.40 |

`hop2` uses the odd frames, whose centres fall on the core's block edges, so the C can make them in
step. The renders Jeff listened to used the even ones, which scored 0.3-0.5 dB better on fricatives
(600: 8.45 dB) and the same on vowels. He heard no difference between the full version, `pair` and
`hop2` (2026-11-07), so the C build is `k6`.

Order 3 matters: order-2 filters leak enough outside their bands that the band error rises by about
2 dB (600: 10.54 dB). Making the filters at 16 kHz rather than on the 8 kHz mirror scores the same.

**Fixed point.** It matches the Python on LibriSpeech 3536 and EARS p020: per band, 0.2-0.4 dB
mean and 1.6-2.2 dB rms frame by frame (two noises, so frames differ). The C's one-poles step
every 20 ms where the Python's step every 10 ms and are then sampled. The light layer's C and
Python differ by the same amount. Random bits, losses and one-frame and empty streams run clean
under AddressSanitizer, UndefinedBehaviorSanitizer and `FX_CHECK`. The default and light builds
are unchanged. Details:

- The noise is xorshift32 (uniform, ±1024). The sections are Q14 with two states each (transposed
  direct form II), and the gains are Q16 below 8.
- The 16 kHz output buffer holds the block's noise and hiss until the last loop overwrites them,
  so the hiss needs no buffer of its own.
- The level interpolations walk the bins in order (no divisions), and the band power sums read
  every other bin (the levels are linear in dB over 250 Hz), with 3 dB added back.

**Cortex-M0 model** (300 frames of LibriSpeech 3536, decoder only; heap is the decoder's state):

| | Mean MHz | Worst frame MHz | Mean, code in RAM | Heap | Stack |
|---|---:|---:|---:|---:|---:|
| v3.7 decoder (plain, what the K6 plays) | 47.0 | 74.9 | 37.3 | 0.8 KB | 2.7 KB |
| light layer | 69.7 | 97.4 | 55.4 | 13.7 KB | 3.2 KB |
| made with the core's output, four filters, gains every 10 ms | 67.3 | 94.7 | 52.6 | 1.9 KB | 3.2 KB |
| **the C build (`pair` and `hop2`)** | **59.3** | **86.7** | **46.8** | **1.8 KB** | 3.2 KB |

The C build's 12.3 MHz: the two filters and gains 4.3, the block loop with the noise and the fold
4.2, stages A and B at 20 ms about 3.8. The core computes its model bands for the odd halves only,
the ones the layer reads.

Where the four-filter version's 20.3 MHz went (with the hiss pieces kept as separate functions,
which costs 1.4 MHz):

| Part | MHz |
|---|---:|
| The four filters (10 sections a sample) | 6.8 |
| Stages A and B (ridge, levels, band power sums, 64-bit helpers) | 7.0 |
| The output fold (`up2_q15`) | 3.3 |
| The block loop, noise and gains | 1.2 |

The two cheaper options took 8 MHz off together. `pair` reuses the 4-5 and 5-6 kHz filters'
output, (−1)ⁿ modulated, for 7-8 and 6-7 kHz (the mirror filters are the same with z → −z), so
those bands share their fine structure; Jeff couldn't hear it. `hop2` runs stages A and B every
20 ms.

**RAM on the K6.** The layer's state is 1.0 KB (the 1.8 KB heap less the plain decoder's 0.8 KB),
plus about 0.4 KB more stack. The 16 kHz PWM ring would grow too: by 0.64 KB at three segments.
That is about 2 KB against the 0.66 KB free while 600 / 400 play. To close the gap:

- Trim the state: the model rings in 16 bits, the speaker-message bits packed. About 0.4 KB.
- Fold in the playback interrupt instead: the ring keeps 8 kHz y8 − s plus an 8-bit s, and the
  interrupt runs `up2` (3.3 MHz moves there).
- Or take 1 KB from the 4 KB message store: 1200 would hold 20 s instead of 27 s, and streaming
  works as now with a shorter ring. That is a choice for Jeff.

**CPU on the K6.** On the radio, code in flash runs about 1.29 times the model. sotto 1200 already
decodes in 16.1 ms of each 20 ms half with RX running, which leaves about 3.9 ms (about 19 MHz):

- With four filters and gains every 10 ms, 20.3 MHz on the model is about 26 MHz on the radio,
  5.5 ms. That doesn't fit from flash.
- The C build, 12.3 MHz on the model, is about 16 MHz on the radio, 3.3 ms: it just fits on
  average, and the worst frames lean on the PWM ring.
- With the hot loops in RAM it is 9.5 MHz, about 2 ms.

All of this needs measuring on the radio. Both RAM and CPU point to the same next step: the layer
in the 1200 / 600 / 400 decoders, which the fixed-point port doesn't have yet. The C here is the
2400 v3.8 layer; its hiss parts are the same.
