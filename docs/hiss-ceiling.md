# Hiss ceiling: s and sh on voices that stop below 8 kHz

Status: prototype and listening round 14, not in the encoder. 2026-11-02. Builds on v3.8
(`docs/unvoiced-hiss.md`).

Jeff's question: the generated high-frequency noise for s and other sibilants still sticks
out. Could the noise envelope follow each speaker's high-frequency spectrum, for example a
speaker whose hiss only reaches 5 kHz and then rolls off steeply?

## What v3.8 already does

Every all-unvoiced (UU) frame carries a 6-bit hiss index: four band levels (4-5, 5-6, 6-7 and
7-8 kHz) as a correction to a guess from the 0-4 kHz envelope. The decoder joins the four
levels with straight lines in dB between the band centres (4.5, 5.5, 6.5, 7.5 kHz) and holds
them flat beyond the ends. So each frame already has a coarse spectral shape. The per-speaker
side channel (3 PCA coefficients) shapes only the vowel high band, not the consonant hiss.

## How well that follows real voices

Sibilant frames (4-8 kHz more than 6 dB over 0-1 kHz, loud), 250 Hz bins
(`tools/hiss_proto/ceiling_study/`):

- **Normal voices.** On 40 LibriSpeech dev-clean speakers and 4 EARS studio speakers, the
  speaker's mean s spectrum inside each 1 kHz band differs from the 4-band line by only
  ±2-3 dB (`within.py`). On the 12 test clips, the decoded hiss matches the original's mean
  sibilant spectrum within about ±3 dB from 3 to 8 kHz (`measure.py`). Its frame-to-frame
  flutter is no larger than the original's. For these voices the shape is already covered.
- **Steep roll-offs.** Three of the 40 LibriSpeech readers (777, 2086, 3170) come from
  MP3 sources cut at 6.75-7.3 kHz. The other kind is a dull microphone, simulated by
  low-passing clips at 5 and 6 kHz with a 10th-order Butterworth (`lp5.py`). v3.8 turns
  the cliff into a gentle slope. The codebook cannot reach -40 to -60 dB, and the 4-band
  line spreads the drop over a whole band:

| Mean sibilant spectrum, dB re 4.0-4.5 kHz | 4.0 | 4.5 | 5.0 | 5.5 | 6.0 | 6.5 | 7.0 | 7.5 kHz |
|---|---|---|---|---|---|---|---|---|
| 8842 low-passed at 5 kHz, original | +0 | +3 | -6 | -43 | -59 | -64 | -66 | -68 |
| v3.8 | -10 | -18 | -25 | -29 | -33 | -39 | -46 | -52 |
| v3.8 + ceiling | -1 | +1 | -4 | -37 | -59 | -64 | -66 | -68 |
| 777 (MP3, cut ~7.1 kHz), original | -0 | +2 | +6 | +3 | +5 | +3 | -23 | -49 |
| v3.8 | +2 | +3 | +4 | +5 | +3 | -1 | -9 | -15 |
| v3.8 + ceiling | +1 | +2 | +3 | +4 | +4 | +2 | -9 | -49 |

So v3.8 adds 15-35 dB of hiss above the speaker's cut-off and loses up to 18 dB just below it.

## The ceiling model

Two numbers per speaker: a cut-off `fc` (4.25-7.75 kHz in 250 Hz steps, or off: 4 bits) and a
steepness `S` (12, 24, 48 or 96 dB/kHz: 2 bits). Above `fc` the whole 4-8 kHz layer
(consonant hiss, mixed-frame hiss and vowel layer) gets `-min(S (f - fc), 60)` dB.
`ceiling_study/ceiling.py`:

- **Encoder, measuring.** Power-average the speaker's sibilant spectra (each frame relative to
  its 4-6 kHz level), then fit a line plus the ceiling for every (`fc`, `S`), with both sides
  floored 50 dB under the peak. Keep the ceiling only if it cuts the rms misfit by 3 dB or more.
  It finds no ceiling for any of the 37 normal LibriSpeech speakers or the 4 EARS clips, and
  finds every cut clip (777 at 7.0 kHz, 3170 at 6.75, 2086 on 3 of 6 files, the 5/5.5/6 kHz
  low-passes at 5.25/5.75/6.0). One false-looking hit: 3576 on 2 of 6 files at 6.75 kHz,
  24 dB/kHz, a real gentle roll-off.
- **Encoder, index search.** With a ceiling, each UU frame's 6-bit index is chosen against the
  250 Hz-bin spectrum the decoder will make (interpolation + ceiling), floored 40 dB under the
  frame's peak, instead of against the four band levels. Without this the index still spends
  its range trying to pull 6-8 kHz down.
- **Decoder.** One gain per FFT bin on the 4-8 kHz layer. In the C decoder this folds into the
  existing per-bin magnitude, so it costs nothing measurable.
- **Cost.** Encoder: one spectrum accumulation (the STFT is already there), a 60-candidate fit
  once per window, and a 64 x 16 instead of 64 x 4 distance search per UU frame (about
  +0.05 MHz). Bits: 6 bits per speaker, which rarely change (they describe the microphone and
  the speaker). Not on air yet; options are below.

With no ceiling found, the bitstream and the output are exactly v3.8.

## Jeff's variants (three bands, harder ceiling, ceiling scale factor)

Unquantised representation error, rms dB over 250 Hz bins on sibilant frames, each frame
floored 40 dB under its peak (`models.py`):

| Clip | 4 bands (v3.8) | 3 bands | 4 + ceiling | 3 + ceiling | 3 in 4 kHz..fc + brick wall | 4 in 4 kHz..fc + brick wall | 3 in 4 kHz..fc + measured slope |
|---|---|---|---|---|---|---|---|
| 777 (cut 7.0) | 6.9 | 13.0 | 4.5 | 4.5 | 5.4 | 5.1 | 4.7 |
| 3170 (cut 6.75) | 5.0 | 8.3 | 4.6 | 4.3 | 7.4 | 7.1 | 3.9 |
| 8842 LP 5 kHz | 5.9 | 4.2 | 2.6 | 4.0 | 4.0 | 3.8 | 2.7 |
| 251 LP 6 kHz | 4.9 | 4.0 | 3.6 | 3.3 | 5.3 | 5.1 | 2.6 |
| 652 LP 5 kHz | 5.6 | 3.3 | 2.0 | 3.0 | 2.9 | 2.6 | 3.1 |
| 6313 (normal) | 2.8 | 3.2 | 2.8 | 3.2 | 3.2 | 2.8 | 3.2 |
| 8842 (normal) | 3.0 | 3.6 | 3.0 | 3.6 | 3.6 | 3.0 | 3.6 |

- Three fixed bands cost normal voices 0.4-0.6 dB and real cut-offs a lot. They only help when
  a ceiling takes over the top band.
- A brick wall is worse than the measured slope on the real MP3 cuts (3170: 7.4 vs 3.9),
  because those fall at 48-96 dB/kHz, not instantly, and `fc` is on a 250 Hz grid. The fit
  already picks 96 dB/kHz (60 dB down within 0.6 kHz) for every low-passed clip.
- Three bands squeezed into 4 kHz..fc with the measured slope is about as good as four fixed
  bands with the ceiling (better on 251 and 3170, a little worse on the 5 kHz clips). Its real
  gain would be in quantisation: three numbers in 6 bits quantise finer than four, or 5 bits
  give the same error and free a bit. That needs a new codebook, so it waits for the round 14 result.
- A depth (scale) factor: every cliff measured so far is 40-60 dB deep, so a fixed 60 dB depth
  works; the 2-bit steepness already covers gentle roll-offs like 3576.

## Listening round 14

https://claude.ai/artifact/3uTgLpQHtnthR1HSDttSEK, `tools/listening/render_ab14.py`, key
`/mnt/project-files/vocoder/listening/ab-14-key.json`. v3.8 vs v3.8 + ceiling (measured from
the whole clip and known to the decoder) on 777, 3170, and 8842/652 low-passed at 5 kHz,
3081 at 5.5 kHz and 251 at 6 kHz, plus 8842 at 5 kHz over 446 MHz / 60 km/h / 52 dB-Hz.
Null pair: 6313, a normal voice, where no ceiling is found and both sides are v3.8.
Spectrograms: `/mnt/project-files/vocoder/hiss-ceiling/spectrograms.png`.

| Pair | Clip | WB-PESQ v3.8 | WB-PESQ ceiling | Above fc, re 0.3-4 kHz: original / v3.8 / ceiling |
|---|---|---|---|---|
| 1 | 652 LP 5 kHz | 1.671 | 1.865 | -74 / -40 / -66 dB |
| 2 | 777 | 2.058 | 2.095 | -72 / -38 / -69 |
| 3 | 3081 LP 5.5 kHz | 1.374 | 1.458 | -71 / -39 / -62 |
| 4 | 3170 | 1.914 | 1.940 | -50 / -42 / -50 |
| 6 | 251 LP 6 kHz | 1.569 | 1.728 | -66 / -38 / -64 |
| 7 | 8842 LP 5 kHz, fading | 1.340 | 1.552 | -68 / -37 / -62 |
| 8 | 8842 LP 5 kHz | 1.395 | 1.550 | -70 / -37 / -64 |

Narrowband PESQ is unchanged (the ceiling only touches 4-8 kHz).

## Getting the two numbers to the decoder (if round 14 wins)

The side channel carries one 16-bit message (12 payload + CRC-4) per 2 s window in the UU
frames' spare bit, used by the vowel PCA coefficients. Options, cheapest first:

1. Odd windows carry the ceiling (6 bits + 6 spare + CRC-4), even windows the vowel shape.
   The vowel shape then locks in about 2 s later.
2. The first window of a transmission carries the ceiling, then every fifth window.
3. With the three-band variant on a 5-bit codebook, each UU frame frees one more bit: a second
   side-channel bit per UU frame, a 16-bit message per window just for the ceiling.

Caveat for the radio itself: if the FTM-300D's AIC3204 runs at 16 kHz, its ADC decimation
filter ends the input band at about 0.45 fs = 7.2 kHz, so every transmission looks like 777.
The receiver's DAC filter also removes the excess above 7.2 kHz, so on the radio only cut-offs
below about 7 kHz (a dull microphone) would be audible. On a PC decoder all of them are.
