# Unvoiced consonants: level, spare bits and 4-8 kHz hiss

Status: prototype and listening round 11, not in the encoder. 2026-11-01.
Measured against v3.6. v3.7, merged while this ran, changes only the pitch tracker (same
decoder and bitstream), so the findings carry over; a v3.8 would build on v3.7. Round 11 (version 2 of the page)
and tools/hiss_proto/ are on v3.7: Jeff spotted v3.6's 490 Hz pitch jump on p020 in pair 1.

Jeff's question: would s, sh and f sound more natural if unvoiced consonants got more
bandwidth, starting from a default spectral template and refined during pauses or low-rate
intervals, with some simple high-frequency hiss shaping?

## What is wrong with s/sh/f in v3.6

Measured over the 16 held-out voices, on frames where 2.5-4 kHz is louder than 0-1 kHz
(`tools/hiss_proto/diag3.py`):

| Step | 0-1 kHz | 1-2 kHz | 2-2.5 | 2.5-3 | 3-3.5 | 3.5-4 kHz |
|---|---|---|---|---|---|---|
| Unquantized model, rate-K envelope | -3.2 | -3.2 | -3.0 | -3.1 | -3.1 | -3.7 dB |
| + 20 ms decimation | -3.2 | -3.5 | -3.1 | -3.3 | -3.5 | -4.0 |
| + 34-bit VQ | -3.3 | -3.4 | -3.4 | -3.4 | -3.4 | -4.0 |
| + postfilter (= v3.6) | -8.5 | -4.1 | -3.4 | -3.1 | -3.8 | -3.1 |

So inside 0-4 kHz the fricatives have about the right shape but are about 3 dB too quiet.
Quantization adds almost nothing. The loss comes from two places:

- Random-phase harmonics overlap-added with the 10 ms triangular window lose 1.76 dB
  (the mean of w² + (1-w)² is 2/3). Codec 2 has the same loss.
- The rate-K envelope samples a noisy harmonic spectrum and interpolates it in dB, which
  averages logs rather than powers: about 1.3 dB more.

Above 4 kHz there is nothing, because the codec runs at 8 kHz. In studio recordings
(EARS) most of the energy of "s" is at 4-8 kHz.

## The spare bits

- **Pauses are not silent.** Energy code 0 (digital silence) is about 0% of frames in
  real read speech, because pauses carry room noise. So "pauses" means all-unvoiced frames.
- **All-unvoiced frames (voicing code UU) are 24% of frames**, and their 7 pitch bits are
  almost unused. The pitch only sets the spacing of the noise harmonics. With a fixed
  50 Hz spacing in unvoiced halves, PESQ is 2.916 against v3.6's 2.914 over 19 voices
  (`p_eval.py`). (A fixed 100 Hz spacing costs 0.024, because 100 Hz harmonics with
  random phase repeat every 10 ms and sound rough.) That frees 7 bits × 24% × 50 frames/s
  ≈ 84 bit/s.
- Mixed (MM) and edge frames still need their pitch, so voiced fricatives like z and v get
  no spare bits.

## Ways to spend them, and what each is worth

| Option | Bits | Result |
|---|---|---|
| Level fix: unvoiced halves +3 dB, 50 Hz noise spacing | 0 | PESQ 2.900 vs 2.914 (within PESQ noise; +1.5 dB is neutral). Needs ears. |
| Better s/sh envelope inside 0-4 kHz (7-bit extra VQ stage in UU frames) | 7 in UU | Ceiling with a perfect envelope in UU frames: +0.03 PESQ. Not worth it. |
| 4-8 kHz hiss, decoder guess from the 0-4 kHz envelope | 0 | Band levels off by 11-14 dB rms on unvoiced frames: about as bad as a constant. |
| 4-8 kHz hiss, 7-bit index (4 band levels, residual of the guess) in UU frames | 7 in UU | Error about 6 dB rms (5.5 LibriSpeech, 6.8 EARS). |
| Per-speaker template, refined slowly in pauses | ~84 bit/s | Ceiling (the speaker's exact mean, on the test clip itself): 14.4 → 12.0 dB on EARS unvoiced frames. Much smaller gain than the per-frame index. |
| Carry the last UU index into mixed frames (z, v) | 0 | 14.8 → 13.9 dB: does little. Voiced fricatives stay narrowband. |

Errors are rms dB over four bands (4-5, 5-6, 6-7, 7-8 kHz) on 12 held-out voices (8
LibriSpeech, 4 EARS). Trained on 32 other LibriSpeech dev-clean speakers (CC BY 4.0); no
EARS audio is used in training.

So Jeff's "default plus adaptive refinement" works best as a default guess plus a
per-frame correction in the frames that are the fricatives, not as a slow template. The
template mostly fixes the microphone and room, which a per-frame index already covers.

Wideband PESQ does not help choose: with the true 4-8 kHz levels, adding noise only to
unvoiced frames lowers WB-PESQ (1.70 → 1.60), while adding it to all frames raises it
(1.86). WB-PESQ punishes noise added where the reference has structure, so this is a
listening question (round 11).

## Prototype (tools/hiss_proto/)

- `proto.py`: decoder with 50 Hz unvoiced spacing and unvoiced gain (uses two new hooks in
  `SottoV0.decode`, `_frame_hook` and `_model_hook`, which do nothing by default).
- `hiss.py`: the hiss7 codec. The encoder takes 16 kHz input, codes the 8 kHz part as v3.6,
  runs its own decoder to get the guess, and writes the 7-bit residual index into the pitch
  field of UU frames. The decoder upsamples to 16 kHz and adds random-phase noise above
  3.9 kHz (sqrt-Hann, 20 ms frames, 10 ms hop) in UU frames and in the unvoiced half of a
  neighbouring edge frame. Lost frames reuse the concealed bits and the concealment fade.
  `hiss_model.npz` is the trained guess (3 × 8 × 4 coefficients) and codebook (128 × 4).
- `render_ab11.py`: listening round 11. Paths in these scripts point at the sandbox
  (`/tmp/claude-0/...`), since the 16 kHz clips are not in the repository.

## Cost on the C5517

- Bits: none added; the frame stays 48 bits. A v3.6 decoder reading a hiss7 stream would
  take the index as a pitch for the noise spacing (harmless but rougher), so this is a new
  stream version.
- Decoder: 2× upsampling (half-band FIR, about 0.2 M MAC/s), one 320-point inverse FFT per
  10 ms for the hiss (the C5517 has an FFT accelerator), and up to 80 instead of 40 noise
  harmonics in unvoiced halves (about 0.6 M MAC/s worst case). About 1-1.5 MHz on top of
  the 4-8 MHz estimate.
- Encoder: 16 kHz input and a 2× decimator, one 320-point FFT per 10 ms for the band
  levels, the guess (32 MACs) and a 128 × 4 search per UU frame. Under 1 MHz.
- Memory: about 1 KB of tables. Everything is dB arithmetic on 4 bands, which fits the
  existing Q8 dB style in `c/fixed/`.
- **Open question: the radio's audio path.** The hiss only matters if the FTM-300D's mic
  path and speaker pass 4-8 kHz. The AIC3204 can run at 16 kHz, but the mic preamp
  filtering and the speaker response are unknown. AMBE runs at 8 kHz, so the stock
  firmware may never have needed more.

## Listening round 11

https://claude.ai/artifact/V8Y9EfZ32XLFFQ8kvXSAqh, key
`/mnt/project-files/vocoder/listening/ab-11-key.json`. Pairs: v3.6 vs level (2), v3.6 vs
hiss7 (5), level vs hiss7 (2), v3.6 vs hiss0 (1), null (1). Original in every pair; 5 of
the 11 pairs over fading 70 cm and 2 m.

Round 11 result (Jeff): the hiss version (hiss7) won every pair answered: 4 of 4 against
v3.7 (two clean, two fading) and 2 of 2 against the level fix alone. The decoder guess beat
v3.7 1 of 1. Level fix vs v3.7: 2 ties, so the 3 dB alone isn't audible. Notes: t (pair 1,
p020) and p (pair 7, p095) need more high-frequency hiss; Jeff asked to keep watching p047
(pair 8), where all consonants sound thin.

## t, p and k bursts (round 12)

Stop bursts usually run straight into the vowel, so their frame is coded mixed or edge and
gets no hiss index. Over the 12 test voices, the first 30 ms after a burst onset (4-8 kHz
jumps by more than 12 dB in 10 ms and is within 15 dB of 0-4 kHz) came out 7.1 dB short
(`bursts2.py`):

| Variant | Burst shortfall, 4-8 kHz | Band error, all live frames | Narrowband PESQ |
|---|---|---|---|
| hiss7 | -7.1 dB | 19.1 dB | 2.810 |
| + decoder guess in mixed frames (-3 dB), "m" | -5.3 | 17.4 | 2.810 |
| + burst frames sent as UU, "b" | -2.8 | 18.9 | 2.801 |
| both, "bm" | -2.4 | 17.3 | 2.801 |

The burst rule turns about 6 frames per 12 s into UU, so they lose 20 ms of low-frequency
voicing at the vowel onset (that stretch is mostly aspiration anyway) and gain an index.

p047 (`p047.py`): its 135 mixed frames outnumber its 86 unvoiced ones. Unvoiced frames are
now right (4-8 kHz within 0.2 dB). Mixed frames are 3 dB low at 2-3.9 kHz and 19 dB short
at 4-8 kHz with hiss7, 10 dB short with "bm". The rest of the gap is the conservative -3 dB
on the guess, and the mixed frames' noise harmonics losing the same 1.76 dB as unvoiced
ones.

Round 12: https://claude.ai/artifact/6Jafb6jidSvJncesyjgS7J, key `ab-12-key.json`:
hiss7 vs bm (5, including p047 clean as pair 10 and fading as pair 8), hiss7 vs b (2), b vs
bm (2), null pair 1.

Round 12 result. Tyler: bm beat hiss7 4-1 (lost the 70 cm fading pair 3), b beat hiss7 1-0
with 1 tie, bm beat b 2-0; he picked a side on the null pair. Jeff: bm beat hiss7 2-0
(p047 fading, p095 clean), b vs bm 1 tie. So bm (burst rule + mixed-frame guess) is ahead,
6-1 against hiss7 overall. Notes from Jeff: pitch warble in pair 9 (p095); its two clips have
identical pitch tracks, so the warble comes from the v3.7 tracker, not the hiss. "act" in
pair 10 (p047) has a scratch at the vowel onset, and the "th" of "the rainbow" is weak.
Across these two clips the burst rule changed only 1-2 frames each. p047 is a bright studio
voice with strong 4-8 kHz in its vowels too, which none of the variants add.

## The "act" scratch (round 12 pair 10, 3.02-3.17 s)

The vowel of "act" starts with a creaky glottal onset after "they". The v3.7 encoder codes
the first 50 ms (3.02-3.06 s) as unvoiced, at the 90th-percentile frame energy, so a loud
vowel onset is played as noise. Both clips of the pair have it. The +3 dB unvoiced boost
makes it louder, and it costs PESQ on v3.7 (2.953 → 2.917 for the level fix alone).
`proto.py` Proto2 tests two fixes (`p2_eval.py`, 19 voices):

| Variant | PESQ | ESTOI |
|---|---|---|
| v3.7 | 2.953 | 0.819 |
| level fix (50 Hz noise, +3 dB on all unvoiced) | 2.917 | 0.809 |
| + boost only unvoiced frames whose envelope tilts upward ("gate") | 2.943 | 0.813 |
| + encoder recodes loud, low-heavy UU frames as MM with the neighbour's pitch (0.6% of frames) | 2.931 | 0.814 |

The gate is a decoder-only rule and recovers most of the loss. The onset recoding fixes
two of the five "act" frames and costs PESQ, so it needs ears; really it is a voicing
decision for the tracker. Snippets (2.5-4.0 s): `/mnt/project-files/vocoder/listening/act/`.

Jeff (2026-11-01) preferred act_3 (gate + onset recoding) as least distracting, so the
current candidate is hiss7 + burst rule + mixed-frame guess + gated boost + onset recoding.

## v3.8 in the model, and vowel brightness (round 13)

`sim/hiss_band.py` SottoV38 is the round 12 winner as a codec class (16 kHz `encode`,
`decode_audio16`), trained by `tools/train_hiss_band.py` into `sim/data/hiss_band.npz`
(LibriSpeech only). Jeff chose one more round before building it into C.

Bright studio voices (p047) also carry 4-8 kHz in vowels, which v3.8 doesn't add. On the
12 test voices (`tools/hiss_proto/vowel_eval.py`), rms band error on EARS / LibriSpeech:

| Frames | Guess only | Per-speaker offset, causal, 1 s refresh | Side channel as built | Ceiling (exact speaker mean) |
|---|---|---|---|---|
| Voiced | 10.6 / 9.8 | 8.0 / 7.3 | 8.6 / 8.3 | 7.2 / 6.8 dB |
| Mixed | 15.6 / 13.8 | 10.6 / 9.3 | 11.1 / 9.8 | 8.5 / 8.2 |
| UU index 7 → 6 bits | 3.9 → 4.2 / 3.8 → 4.0 | | | |

This is Jeff's original "default, refined in pauses" idea, and it fits vowels much better
than fricatives. The side channel takes one bit from each UU frame's index (6 bits left):
each 2 s window (100 frames, frame numbers known to both ends) carries a 16-bit message in
its first 16 UU frames, made of level (4 bits, 2 dB steps) and tilt (2 bits) for voiced and
for mixed frames, plus CRC-4. The message describes all earlier windows, so the encoder
needs no look-ahead, and it applies from the next window. A window with a lost frame, or
fewer than 16 UU frames (47% of EARS windows, 15% of LibriSpeech), keeps the old offsets.
Offsets start at the default (the guess), so the first ~4 s use it.

Round 13 (https://claude.ai/artifact/WffNrXkvcz13cgGkKNWwLr, `ab-13-key.json`,
`tools/listening/render_ab13.py`): C (no vowel high band) vs G (guess - 3 dB in voiced
frames, no bits) vs A (adaptive offsets), null pair 1.

Round 13 result (Jeff): adaptive beat C 4-0 (pairs 2, 5, 7, 8), adaptive vs guess 1 win and
2 ties, guess beat C 1-0, null correct. Notes: adaptive sounds "scratchy" but clearer and a
better blend of voiced and unvoiced; the ideal is between adaptive and none. Follow-up
decoder options in SottoV38: `voiced_trim` (dB on voiced frames' high band) and
`texture="fold"` (voiced/mixed high band = the 0-4 kHz spectrum mirrored about 4 kHz,
scaled to the band levels, so it keeps the pitch pulses instead of plain noise). Snippets
(4-10 s of pairs 2 and 3): `/mnt/project-files/vocoder/listening/vowel/`.

Follow-up on "scratchy": the -4 dB trim and the folded texture didn't help (folded was
"terrible"; Jeff: "scratchy high harmonics, like a tambourine full of BBs"). Measured on
the vowel frames of four voices, adaptive put 5-7 kHz 4-10 dB above the original, with a
bump the original doesn't have: EARS vowels sit near the 3.5-4 kHz level at 4-5 kHz, then
drop about 11 dB above 5 kHz. Two fixes in SottoV38:
- the +3 dB unvoiced boost now applies only to frames whose index comes from a UU frame
  (it had been added to voiced and mixed frames too);
- `vowel_shape="template"`: voiced frames' high band = the decoded 3.0-3.5 kHz level +
  a fixed per-band template + a term in the vowel's own 2-3.5 kHz slope (trained on
  LibriSpeech vowels, `vtemplate.py`; template -8, -15, -18, -21 dB). That follows each
  vowel's shape and sits at or a little below the original. Snippets 7 and 8 (template,
  and template -4 dB) in `/mnt/project-files/vocoder/listening/vowel/`.
The pitch-pulse texture (`texture="pulsed"`) overshoots the original's pulse depth (0.54
vs 0.42) and wasn't sent.

Jeff on snippets 7/8: the vowel high band pops in and out and adds no clarity; he chose one
more try. `vowel_shape="smooth"`: one continuous vowel layer (template levels smoothed
over ~30 ms, faded by a smoothed voiced/mixed weight), no side channel, UU index 7 bits;
mixed frames no longer get the guess separately. Snippets 9 (-4 dB) and 10 (-8 dB). Jeff
suggests a better texture: a whispered vowel keeps its resonances, so the high band should be
noise with resonance peaks ("a high quiet whistle within the vowel envelope"), not flat bands.

Jeff: snippet 10 (smooth layer, -8 dB) improved both pairs. His bench recording (voiced and
whispered vowels): voiced vowels sit 8-15 dB below 3-4 kHz above 4 kHz, with one resonance
per vowel at 4.9-7.3 kHz (mostly 5.0-5.3 or 6.0 kHz); whispered vowels have the same
shape. Per-speaker peaks in the test voices: 4.55-7.1 kHz, 4-14 dB high. Option
`peak_hz`/`peak_db`: a Gaussian (250 Hz) resonance on a 4 dB lower floor in the smooth
layer. Comparison page (his recording, pairs 2 and 3; v3.7, consonants only, flat layer,
whistle): https://claude.ai/artifact/2grQXFJWZTR6v4SfwhiUQ4. The peak is measured from the
original here; in a link it would go over the side channel (4 bits frequency, 2 bits height).

## Whispers, blending and the v3.8 candidate

Two more bench recordings (Jeff on his phone, his wife on hers) settled the vowel layer.
The high band's loudness is mostly the microphone (Jeff's vowels are 10-15 dB brighter
above 4 kHz on the phone than on the desk mic); the resonance's position is the voice, but
it moves with the vowel (Jeff ~5.1 kHz in front vowels, ~6.0 kHz in back vowels; his wife
4.3-6.6 kHz). One fixed peak per speaker is too simple, so `vowel_shape="fine"`: a ridge
regression from the decoded 0-4 kHz shape (250 Hz bands re 3.0-3.5 kHz) to sixteen 250 Hz
bands at 4-8 kHz, trained on LibriSpeech vowels (`vfine.py`, `sim/data/hiss_vowel_fine.npz`),
plus a per-speaker correction compressed to 3 PCA coefficients of the speaker's mean miss
(`vfine2.py`: 3 coefficients explain 92% of the between-speaker variance; held-out error
8.4 dB vs 11.9 for the vowel-only predictor and 13.0 for a flat template). The 3 numbers
ride the slow side channel (`vowel="speaker"`: 4 bits each in 4 dB steps + CRC-4, one
16-bit message per 2 s window in the UU frames' spare bit, the UU index drops to 6 bits).
The encoder estimates them causally over vowel frames above the running 60th percentile of
frame energy (1 s warm-up; a 1 dB histogram in C), which lands within 0.6 dB rms of the
whole-clip estimate the layer was tuned with. A message applies from the next window, so the
first 2-4 s of a transmission use the average speaker.

Jeff's notes on the whistle page, and the fixes, in order:

- **Low warble under whispered vowels.** v3.7 codes a whisper as mixed: the tracker finds a
  pitch in noise and the decoder plays wandering harmonics below 1 kHz. In his desk-mic
  whisper 93% of frames were mixed. `whisper=True` (encoder, `_whisper`): a run of >= 10
  mixed/edge frames whose median harmonic SNR below 1 kHz is < 6 dB and whose median
  pitch jitter is > 0.025 (log, per 10 ms) is sent as UU. Whispers go 85-93% UU; real speech
  changes 0-12% of frames (mostly background between words); PESQ-NB over the 12 test clips
  2.854 -> 2.867.
- **Hiss popping on and off in her whispered "ah"/"ooh".** The whisper flickered between UU
  (consonant layer) and mixed (vowel layer). Besides the whisper rule, `blend=True`: one hiss
  layer per bin = the louder of the consonant hiss and the vowel layer, and flickers <= 40 ms
  inside a vowel keep the vowel layer on. A 20 ms release on the consonant part sounded like
  "a hard plate reverb"; `release_ms=0` was "clearly better every time".
- **Stutter on the "zh" of "division".** v3.7 flutters V/M/edge every 20 ms through a voiced
  fricative; with hiss above 4 kHz switching with it the stutter is easier to hear.
  `vsmooth=True` (encoder, `_vsmooth`): edge blips <= 2 frames inside a voiced run become M,
  and frames whose 7-frame neighbourhood switches V<->M >= 3 times become M. PESQ 2.869 ->
  2.876 (per clip -0.08..+0.08). Jeff: no problems introduced.
- **Weak "s" in "prism"; warble at the start of her "S".** The no-tail blend had dropped the
  mixed frames' guessed hiss (`keep_mixed=True` restores it), and an "s" that starts inside
  a mixed run kept the vowel's pitch for a few frames. `sib_db=3` (encoder): a mixed/edge
  frame whose 4-8 kHz band is more than 3 dB above 0-1 kHz is sent as UU. Changes 0-3.5% of
  frames; PESQ up where it changes anything (p095 2.92 -> 2.97) except 251 (-0.03). Jeff:
  clear improvement on p095, less clear on p051; the fading LibriSpeech female "really
  really good".

Page: https://claude.ai/artifact/BBK2dLqgTY7zSmFHgDKjba (versions 1-7; version 7 is the
v3.8 candidate with the speaker shape measured from the original; version 8 the same with
the shape over the side channel).

Still open from his notes, worked on before the C port:

- **The lost "t" in "sunlight strikes" (p095 1.06-1.22 s).** The closure and the burst are
  timed right in the decoded output, but the unvoiced frames of the burst and the "s" are
  10-15 dB short at 200-1200 Hz (rate-K envelope vs the encoder's), with the 2-4 kHz
  bands matching: the formant postfilter (1.35) deepens the low-band "valley" of a
  fricative and leaves a thin hiss where the original has a broadband burst. (The original's
  strong 0-100 Hz content through the closure is rumble below the envelope's 200 Hz start
  and is not the problem.) Decoder options `uv_pf` (postfilter strength on UU frames) and
  `onset_pf` (on the first two unvoiced/edge frames after a voiced or mixed frame). Neither
  pays by the numbers: PESQ-NB over the 12 clips 2.887 with the postfilter everywhere,
  2.873 / 2.852 / 2.795 with 1.2 / 1.1 / 1.0 on all UU frames, 2.869 / 2.849 with 1.15 /
  1.0 on onset frames only, and the burst frame's 200-1200 Hz deficit only drops from 6-8
  to 4-5 dB (the following "s" frames stay 13-14 dB short). Both off by default; the burst
  variant is on the v3.8 page for ears. The "t" stays a 20 ms-frame limit.
- **Vocal fry** ("rainbow" on p095 4.28-4.45 s, "light" on p051 6.1-6.45 s): the tracker's
  pitch wanders 72-119 Hz frame to frame through creak (harmonic SNR 2-7 dB) and the
  harmonic synthesis makes a warble. Re-pitching flagged stretches on a line and sending
  them mixed (`fry=True`) cost PESQ 0.05-0.18 on three voices and was dropped. `fry="slew"`:
  inside a voiced run the log pitch may move at most 0.08 per frame unless the frame's
  harmonic SNR is >= 8 dB (one state variable, decoder unchanged). It smooths both spots
  (p095: 82 105 117 103 105 -> 85 91 98 103 105) but costs PESQ 0.015-0.03 on average
  whatever the thresholds (`fry_eval2.py`: step 0.08-0.15, trust 5-8 dB; 3-8% of frames
  touched), with single clips moving +-0.1. Off by default; on the v3.8 page as an A/B for
  the two spots. A proper fix is aperiodic (pulse-by-pulse) excitation in creak, which the
  sinusoidal model can't do, or a tracker that penalises jumps when the harmonic fit is poor.

## Scores and the v3.8 page

0-4 kHz core (PESQ-NB / ESTOI at 8 kHz, `pesq_v37_v38.py`), v3.7 vs the v3.8 candidate's
bits decoded without the high band:

| clip | v3.7 | v3.8 |
|---|---|---|
| ears p020 m | 3.011 / 0.807 | 3.059 / 0.797 |
| ears p047 f | 2.884 / 0.846 | 2.861 / 0.841 |
| ears p051 f | 2.773 / 0.843 | 2.663 / 0.833 |
| ears p095 m | 2.932 / 0.817 | 2.926 / 0.814 |
| libri 2428 m | 3.288 / 0.842 | 3.348 / 0.837 |
| libri 251 m | 2.952 / 0.856 | 2.973 / 0.856 |
| libri 3081 f | 2.493 / 0.700 | 2.511 / 0.702 |
| libri 3536 f | 2.945 / 0.819 | 2.858 / 0.820 |
| libri 6313 f | 2.929 / 0.796 | 2.662 / 0.782 |
| libri 652 m | 2.730 / 0.810 | 2.870 / 0.809 |
| libri 777 m | 3.152 / 0.811 | 3.140 / 0.801 |
| libri 8842 f | 2.862 / 0.839 | 2.765 / 0.835 |
| **mean** | **2.913 / 0.815** | **2.887 / 0.810** |

Mean -0.026 PESQ, with 6313 -0.27, p051 -0.11, 8842 -0.10 and 652 +0.14. Ablations
(`ablate_6313.py`, `hook_ablate.py`, `spacing_ablate.py`): the encoder rules account for
little of it (6313: v3.7 bits through the v3.8 decoder 2.732, + onset recode / burst /
whisper 2.721, + vsmooth 2.662, + sibilant 2.662); most is the decoder's unvoiced synthesis
change, 50 Hz noise spacing in place of v3.7's coded-Wo / 100 Hz halves, which PESQ
dislikes on 6313, 8842 and p095 (-0.1..-0.2) and likes on 652 and p047 (+0.1), mean -0.03
before the gated +3 dB boost and -0.014 after. With 100 Hz spacing the mean is 2.911, level
with v3.7. 50 Hz is kept: at 100 Hz the random-phase sum is periodic within the 20 ms frame
(a 10 ms buzz under fricatives, the old Codec 2 unvoiced texture), and the hiss variants
built on 50 Hz won rounds 11-13 and the whisper page by ear, including on p095 where PESQ
says the opposite. PESQ-NB moves by +-0.1 per clip on changes to noise synthesis, so the
per-voice numbers above are not to be read closely.

Page: https://claude.ai/artifact/4znczJEufu9VwqX83JmQpb (9 groups: the three whispered
and two voiced bench clips, the fading LibriSpeech female, p051, p095, p047; original /
v3.7 / v3.8 with the speaker shape measured / v3.8 with the shape over the side channel,
plus the fry and burst A/Bs on p051 and p095). Render: `snip17.py`.

v3.8 as proposed (updated 2 Nov 05:30 UTC): encoder `SottoV38(vowel="speaker",
whisper=True, vsmooth=True, sib_db=-10.0, sib_voiced=True)` (plus the onset recode and
burst rule that are always on), decoder `SottoV38(vowel="speaker", vowel_shape="fine",
voiced_trim=-5, blend=True, release_ms=0, keep_mixed=True)` with `UU_WO_GUARD` (always
on). Same 48-bit frame; UU frames carry a 6-bit hiss index and one side-channel bit in
the pitch field. The side channel warms up for 0.5 s and a message applies from the frame
that completes it.

## Decoder-only experiments on the v3.8 bits (2026-11-02)

Same bitstream, PESQ-NB over the 12 clips, v3.8 decoder as above = 2.886
(`tools/hiss_proto/play1.py`, `play2.py`).

- **Low-frequency roll-off under the 50 Hz noise spacing.** The rate-K grid starts at
  200 Hz, so the 50/100/150 Hz noise components take the 200 Hz level. Rolling them off
  (12 dB/oct below 150 or 250 Hz: -0.005, -0.012) or dropping them (-60 dB: +0.002,
  6313 +0.02) changes nothing. They are not why 6313 and 8842 dislike 50 Hz spacing;
  that cause is still open.
- **Decoder-side pitch smoother for creaky stretches.** Median-5 over log Wo inside voiced
  runs, replacing frames that are off by more than 0.12 or 0.2, optionally only on frames
  6 dB under the run's peak: -0.011, -0.010, 0.000 (gated). p051, the clip with the
  creaky "light", loses 0.04-0.06 every time. The decoder cannot tell creak from a real
  pitch move; only the encoder sees the harmonic SNR. Dropped.
- **Stop-burst shape** (`burst_pf`, hook in `vocoder_lab.SottoV0.decode`): an unvoiced
  frame 8 or 12 dB above the frame before keeps its broadband shape instead of the
  postfiltered one: -0.025, -0.023, -0.014 (pf 1.15). Dropped. It also does not reach the
  "t" of "strikes": that closure sits in M/edge frames at 44-47 dB and the jump into the
  fricative is 3 dB, so no level rule finds it. (The input RMS there is <100 Hz rumble,
  -17 to -36 dB, which hides the burst in any frame-energy view.)
- **Sharp attack** (`attack_db`, decoder option on `SottoV38`): an unvoiced frame 12 dB
  or more above the frame before keeps the interpolated 10 ms subframe at the quiet level,
  a step instead of a half-way ramp: **+0.009 mean, 7 wins / 3 losses**, p095 +0.06,
  6313 +0.05, 652 +0.02, p020 -0.045, 2428 -0.04. Threshold 10 dB +0.003, 14 dB -0.008,
  16 dB -0.016; putting the subframe at the loud level instead ("early") -0.054. It fires
  6-20 times per clip, at fricatives and bursts after pauses (on p095 at 1.82, 2.04, 2.72,
  3.0, 4.56, 6.96, 8.32 s), never inside a word. On the v3.8 page as row 6 for p095, p051,
  p047 and the wife's voiced clip (her opening S). Off by default until heard.

## Where the next real gains might be

Ranked by expected audible gain per hour, with what each would cost.

1. **Stops (the lost "t").** 20 ms frames with 10 ms interpolation cannot place a 10 ms
   burst, and the closure before it is coded as the vowel's tail. Routes: a decoder rule
   (`attack_db`, above, does not reach this case); an encoder rule that codes a detected
   closure as energy code 0 (silence) and pushes the burst's level into the next frame,
   encoder-only and bitstream-compatible but the closure detector must not fire on quiet
   vowel tails; or a 1-bit hold/ramp flag, which needs a bit the UU frame no longer has
   (6 index + 1 side). Ears only; PESQ does not see bursts.
2. **The 50 Hz spacing cost on 6313 and 8842** (-0.2 each, the whole v3.7 -> v3.8 mean
   loss). Not LF content. Next suspects: the overlap-add of independent random phases per
   10 ms subframe at 50 Hz spacing and PESQ's time alignment. Measured on 6313 and 652:
   the UU frames' band levels (0-1, 1-2, 2-3, 3-4 kHz, against the original) and their
   2.5 ms envelope spread are the same at 50 and 100 Hz to 0.1 dB, so it is not level or
   modulation depth; what PESQ reacts to is the texture itself (the 10 ms periodicity at
   100 Hz spacing scores as "more speech-like"). A per-frame disturbance trace from PESQ
   would confirm it; if so the 0.2 is a metric artefact and nothing to fix.
3. **Vocal fry.** Only the encoder sees the creak (harmonic SNR 2-7 dB, pitch wandering
   72-119 Hz). The slew limiter (`fry="slew"`, on the page) is the bitstream-compatible
   fix; PESQ -0.015..-0.03. Anything better needs a creak-aware tracker.
4. **Mixed frames have no hiss index** (z, v, zh, breathy vowels keep the -3 dB guess).
   Their 7 pitch bits are real pitch. A 2-bit tilt could come from the energy field
   (5 -> 4 bits, 2 dB steps) or the shape (34 -> 33) in MM frames only. Bitstream change,
   v3.9 material; worth a PESQ + ears study.
5. **Spare side-channel capacity.** About 24 UU frames per 2 s window carry 16 bits.
   The other ~8 could hold a station hiss-level offset (if PC1 does not already carry
   it), a whisper-mode flag, or a second message per window so the speaker shape locks in
   twice as fast after a fade.
6. **Perceptual weighting in the shape VQ search** (formant peaks over valleys).
   Encoder-only, PESQ-blind, ears.
7. **Voiced phase model.** c2sim `--phase0` is zero-phase per harmonic; a dispersion
   filter or Codec 2's newer phase synthesis would change the voiced texture. Independent
   of the hiss work, large, ears.

### The "t" of "strikes", taken apart (p095, 1.10-1.20 s)

10 ms energies from the analysis: vowel tail 63.8 / 60.2 / 55.8 / 48.9, closure 44.4 /
38.3 at 1.12-1.13 s, burst 43.8 / 49.2, then the s at 53-56 dB. The decoder's level track
(43.8 -> 53.1 with the odd subframe interpolated to 48.5) is right to 1 dB; the burst is
not lost in the energy field. Band levels against the original, per 10 ms, 1.10-1.20 s:

| | 200-1200 Hz | 2-4 kHz |
|---|---|---|
| v3.7, postfilter 1.35 | 3 2 -5 -15 -10 -11 -8 -14 -14 -15 | 2 5 5 -2 2 -11 -6 -4 -3 -7 |
| v3.7, no postfilter | 3 -2 -1 -9 -1 1 8 -3 -5 -4 | 6 4 1 -4 1 -6 -10 -1 -2 -5 |
| unquantised model | 5 2 1 -6 -4 0 6 -1 -2 0 | 2 -1 1 -1 2 -8 -9 -2 -3 -6 |

The shape quantiser is not the problem (no-postfilter and unquantised agree within a few
dB); the formant postfilter takes 8-15 dB out of the burst's 200-1200 Hz, which is what
turns "strikes" into "srikes". The burst frames are broadband: their 200-1200 Hz level
minus their 2-4 kHz level ("tilt") is +6.8 and +2.1 dB, where the s frames that follow
are -6 to -13 dB. 79% of all UU/edge frames have a positive tilt (pauses, breath,
background), so tilt alone is no selector.

Decoder rules on the v3.8 bits (`flat_pf_db`, `flat_onset`, `flat_pf` in
`vocoder_lab.SottoV0.decode`; `play2.py` set 3), PESQ-NB over 12 clips, base 2.886:
no postfilter on every UU/edge frame with tilt >= 0 dB: -0.033 (0 wins / 11 losses);
tilt >= -5: -0.064. Only within two frames of a voiced/mixed frame (the onset gate
`onset_pf` used): tilt >= 0 -0.020, tilt >= -5 -0.020, **tilt >= -5 with postfilter 1.15
instead of none: -0.004 (6 wins / 4 losses), p095 +0.056, p047 +0.026, p051 +0.027.**
The last is on the v3.8 page as row 7 for p095 ("burst, flat onsets only"); the earlier
row 5 (`onset_pf=1.0`, every onset frame, -0.038) is the same idea without the tilt
gate. PESQ likes the postfilter on unvoiced frames in general, so this is as far as a
decoder rule goes; the next step for stops, if the ear wants more, is to spend bits
(a per-frame postfilter-off flag) rather than infer.

## Jeff's first listen to the v3.8 page, and what it turned up (2026-11-02)

Jeff: p047 rows 2/3/4 indistinguishable (expected: her over-the-air shape lands within
4 dB of the measured one; the attack rule touches nine onsets by 10 ms). p095 rows 2-7
indistinguishable and "sunlight" still "sunlike". p051 "prism" still "prithm" in rows 2-5.
On the fading LibriSpeech pair, row 2 (measured shape) clearly beats row 3 (shape over
the air) at "poems", 1 s into the snippet, and row 2 is "the most clear of all the
clips". Three findings came out of chasing those.

**The side channel locked in late.** The message in window w carries the estimate from
windows 0..w-1, and the estimate needs 20 vowel frames after a 1 s warm-up, so the first
real shape reached the decoder at 6 s (8842: windows 0-2 sent the mean speaker); the
snippet starts at 4 s. Two bitstream-compatible changes: warm-up 0.5 s (every test clip
now sends a real estimate in window 1), and the decoder applies a message from the frame
that completes it (its 16th unvoiced frame) instead of the next window boundary; a lost
frame before that drops the message, lost frames after it no longer do. First shape now
at 2.9 s (8842), 3.2 s (p051), but 8.6 s on p095: his windows have 9-11 UU frames, fewer
than the 16 bits a message needs, so windows 1-3 carry nothing. Next step there: 8-bit
messages (2-bit coefficient id, 4-bit value, CRC-2) sent round-robin, PC1 twice as often.

**The pitch field leak (a real bug, fixed).** `interp_wo` gives a voiced odd subframe
whose own first half is unvoiced the *next* frame's Wo. In v3.8 the next frame, when UU,
carries the hiss index in that field, so the mixed half of an edge frame before a
fricative was synthesised at a random pitch for 10 ms (p051: L 39 -> 78 at 7.38 s; three
such subframes in 12 s). `SottoV38.UU_WO_GUARD` keeps the frame's own Wo in that case;
v3.7 decode is bit-identical. It also explains why fresh v3.8 encodes scored up to 0.05
away from the cached `encode_low` bits with identical voicing, energy and shape (those
cached bits still carry the real Wo in UU frames, so they never leaked). Measured on
fresh encodes (`play7.py`): the guard changes 0-9 subframes per clip, 40 in all, and
moves PESQ by up to +-0.1 per clip either way (p020 -0.105, 777 +0.076), mean 2.875 ->
2.868. Ten milliseconds of wrong pitch moves PESQ that much because the phase track of
the voiced run after it shifts; it is also a reminder that per-clip PESQ differences
under 0.1 are noise for this codec.

**"prism" is a z.** Its frames (8.12-8.19 s) have the 4-8 kHz band 2-8 dB *under* 0-1 kHz,
so the sibilant rule (+3 dB) never fires; they are coded M/edge, correctly, and the hiss
they get is the mixed-frame guess. Lowering the threshold (`sib_db` 0 / -3 / -6, fresh
encodes, `play6.py`): -6 recodes 1-11 M/edge frames per clip and costs 0.03 PESQ against
0 (p095 -0.11, 8842 -0.08, p047 +0.09); it is on the page as p051 row 6 ("z as hiss").
The real answer is item 4 of the list above: a hiss level for mixed frames.

**Encoder burst contrast** (`contrast_db`, `vocoder_lab.SottoV0.encode`): code a closure
at its quieter 10 ms half and the burst after it at its louder half. Fires 17-21 times a
clip, p095 1.12/1.14 s 43.9 -> 39.4 / 48.4 dB. PESQ -0.043 (1 win / 9 losses); p095 row 8
for ears.

**Correction on "prism" (05:05 UTC).** The z analysed above (8.12-8.19 s) is the z of
"rainbow IS a division"; Jeff caught the diagnosis clip saying the wrong words. "Act as a
prism" is at 4.3-4.9 s of p051 and its z (4.76-4.82 s) is coded VV by both codecs: her
voice bar stays at 18-23 dB (5 dB under the vowel) while the 6-8 kHz hiss sits at +14 to
+18 dB, nearly as loud. A voiced frame has no hiss index and the vowel layer predicts a
dark vowel, so v3.7 and v3.8 put that hiss 30-50 dB low: the soft "th". Diagnosis group on
the page: original / v3.7 / v3.8 / codec 0-4 kHz + original 4-8 kHz (the ceiling for a
voiced-frame hiss level) / original 0-4 kHz + codec 4-8 kHz / original without its high
band / v3.8 with those z frames sent as UU (`sib_voiced=True`, `sib_db=-10`: 16 frames
recoded in 12 s, three of them the prism z). The encoder's 10 ms measure puts the z at
-10 to -6 dB (4-8 kHz against 0-1 kHz), the 20 ms STFT bands at about 0 dB; the rule's
threshold is calibrated to the former. This is item 4 of the list (a hiss level for
voiced/mixed frames) made concrete.

**Correction on the "t" of "sunlight strikes" (05:10 UTC).** Jeff gave the time index:
0.281-0.521 s of the p095 snippet (= of the clip), not 1.1 s. There the t of "sunlight" runs
into the s of "strikes": 0.36-0.44 s is one 80 ms hiss cluster at +16 to +22 dB in 4-8 kHz
with the voice bar still at 10-20 dB. Four of its five frames are coded M (voice bar
present), one UU, so only one frame gets a hiss index and the rest the -3 dB guess: v3.8's
4-8 kHz there is +23/+19 on one frame and -3..-9, +3/-4 on the others, 20 dB low; v3.7 has
nothing. That single frame of hiss is the "k" in "sunlike". Everything in the two sections
above that was aimed at 1.1 s (`uv_pf`, `onset_pf`, `burst_pf`, `flat_pf_db`,
`contrast_db`, `attack_db`) was aimed at a frame that was fine; those hooks are off by
default and are to be removed. The fix is the same as for prism: frames whose 4-8 kHz
comes within 10 dB of their 0-1 kHz are sent UU with an index whatever their voicing
(`sib_voiced=True, sib_db=-10`). PESQ over 12 clips (`play8.py`, fresh encodes): 2.868 ->
2.844 (3 wins / 6 losses; p051 -0.07 for its three z frames, 251 -0.08, 777 -0.10); the
same threshold on M/edge frames only costs the same, so the cost is the lower threshold,
not the voiced frames. Jeff: prism fixed ("YAY"); p095 at 0.28-0.52 s on the page as
"v3.8 + z fix" for his ears. Lesson recorded: get the time index from the listener before
analysing, and quote it back.


## Cleanup (2 Nov 05:30 UTC)

With the time indices in hand, the hooks that were aimed at the wrong frame came out:
`uv_pf`, `onset_pf`, `burst_pf`, `flat_pf_db`/`flat_onset`/`flat_pf`, `contrast_db` from
`vocoder_lab`, and `attack_db`, `fry` (`_fry`, `_slew`) from `SottoV38`, with their
study scripts. `vocoder_lab.py` is now main plus the one-line pitch guard in `interp_wo`;
v3.7 decode is bit-identical. The z fix is in the candidate: `sib_voiced=True` with
`sib_db=-10`. Jeff: "all of the clips with 'z fix' sound great"; the t of "sunlight" is
unchanged by it and is parked as a frame-size limit (the hiss there now matches the
original within 2-3 dB per 5 ms, so whatever cue is missing sits in the 0-4 kHz vowel
offset). The sections above keep the dead ends as a record.
