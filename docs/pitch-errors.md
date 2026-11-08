# Pitch errors at low rates (2026-11-07)

sotto 400 sends one straight pitch line per 120 ms, so every pitch mistake there is heard for
longer than at 2400. Jeff suggested using the 400 renderings to find the pitch errors and to
fix them in the encoder, where the pitch tracker is shared by every rate.

## The finder

`python tools/pitch_errors.py [--modes track,2400,1200,600,400] [--json OUT]` compares the
pitch each decoder actually plays, frame by frame every 10 ms, against librosa's pYIN
(50-500 Hz) on the same 8 kHz audio. It uses the 16 held-out voices in
`/mnt/project-files/vocoder/samples-clean/` and counts only frames above -40 dBFS that pYIN's
HMM calls voiced.

| Count | Meaning |
|---|---|
| gross | more than 1.6 times off (octave-type) |
| off20 | more than 20% off (the usual gross pitch error, GPE) |
| cents | RMS error of the rest |
| miss / false | voicing disagreements |

It lists the worst runs as time indexes. pYIN isn't ground truth. It sits at its 50 Hz floor
on creaky ends of phrases, and its voicing calls at 8 kHz are loose, so the miss and false
counts are only a guide. A listed spot is a place to listen, not a verdict.

## What it found

Off by more than 20%, out of 8,800 voiced frames:

| | tracker | 2400 | 1200 | 600 | 400 |
|---|---:|---:|---:|---:|---:|
| before | 247 | 243 | 275 | 356 | 472 |
| after the two fixes below | 247 | 243 | 259 | 285 | 296 |
| gross (1.6x), before → after | 151 | 152 | 157 → 146 | 210 → 157 | 206 → 141 |

Before the fixes, sotto 400 almost doubled the tracker's errors. Two encoder bugs caused most
of the extra.

**1. At voice onsets the low rates coded the silence's pitch.** A 20 ms frame whose first half
is unvoiced and second half voiced (voicing code 3, an onset edge) took its pitch from the first
half. That pitch is whatever Codec 2 guessed in the silence, often its 50 Hz floor. The 2400
decoder never plays it, because it takes the next frame's pitch for that half. The low rates
code it, though:

- sotto 1200's B frames interpolate from it;
- the 600 / 400 line fit counts it as a voiced frame, so one onset bends a whole 80 or 120 ms
  pitch line.

Examples in sotto 400:

| Voice | Time | What happened |
|---|---|---|
| LibriSpeech 3536 | 0.63 s | 160 Hz for a 364 Hz voice |
| LibriSpeech 251 | 11.42 s | 136 Hz for 202 Hz |
| LibriSpeech 8842 | 5.94 s | 68 Hz for 222 Hz |
| EARS p051 | 1.94 s | 143 Hz for 219 Hz |

Fix (`Sotto1200.ONSET_WO`, `c/sotto.c emit`): an onset edge codes its voiced half's pitch. The
2400 frame is unchanged, and so are the decoders and the bitstream format.

**2. One wrong frame bent the whole pitch line.** The 600 / 400 line is a least-squares fit, so
a single octave slip pulls every frame of the superframe. Examples:

| Voice | Time | What happened |
|---|---|---|
| LibriSpeech 251 | 6.38 s | 182 Hz for 95 Hz over 9 frames |
| LibriSpeech 251 | 4.14 s | 204 Hz for 97 Hz |
| LibriSpeech 6313 | 4.67 s | 388 Hz for 256 Hz |

Fix (`SottoVLR.OUTLIER = 20`, `c/sotto_vlr.c`):

- A voiced frame more than 20 index steps (about half an octave) off the line is left out, and
  the line is refitted, when at least 2 frames remain.
- In the line search, each frame's error is capped at the same 20 steps.

Smaller thresholds (6 and 10 steps) cut into real pitch movement and scored worse.

### Scores

Scored on the same 16 voices, with the Python encoder and decoder.

| | 2400 | 1200 | 600 | 400 |
|---|---|---|---|---|
| PESQ-NB / ESTOI before | 2.937 / .817 | 2.578 / .762 | 2.096 / .690 | 1.826 / .637 |
| after | 2.937 / .817 | 2.608 / .763 | 2.133 / .692 | 1.861 / .641 |
| hiss variants before (`tools/eval_lr_hiss.py`, `tools/eval_vlr_hiss.py`) | | 2.517 | 2.094 | 1.828 |
| hiss variants after | | 2.544 | 2.113 | 1.854 |

The hiss scores are PESQ-NB from the 16 kHz eval scripts, whose plain 1200 row went from 2.545 to
2.565. Most of the gain is the onset fix (+0.03 at every low rate). The line fix adds +0.016 at 600.

C is bit-exact with Python, as before:

- `tools/check_vlr.py`: 600 99.91%, 400 99.93%.
- `tools/check_lr.py`: 1200 99.6%.
- `tools/check_lr_hiss.py` and `tools/check_vlr_hiss.py`: 99-100%.

The remaining misses are the fixed-lag tracker's, as before. The hiss encoders (`c/sotto_hiss.c`)
take the low rates' pitch from the 2400 frame bits, so they patch an onset frame's pitch bits from
the core (`onset_wi`), as the Python hiss classes inherit `Sotto1200.params`.

The browser page's `docs/sotto.wasm` was rebuilt with the new encoder. Its stored sample renders
(`docs/samples/v38/`) are still from the old encoder, so `make -C c/wasm check` reports the hiss
encoders as differing from them until those are re-rendered.

## Creak at phrase ends (2026-11-08)

Jeff heard the K6 play LibriSpeech 198 (*Sense and Sensibility*, the hiss bench's female clip)
at sotto 400 and reported pitch trouble on "go by all means, my dear", which ends the clip
(11.9-13.9 s). On the K6, the same clip was "much better" at 1200, and 600 "isn't bad". His
read was that the coarse pitch coding gets stuck on vocal fry.

The tracker and every rate get "by" (12.20-12.46 s) right. It's a 9-semitone fall, 287 to
169 Hz, and sotto 400's two straight lines follow it to within about 90 cents. The slip is on
"my" (13.20-13.42 s). The voice goes creaky at the end of the vowel, and then into fry on
"dear" (pYIN 60-67 Hz). The tracker reads the creak as 89-96 Hz, half the voice's 165 Hz. One
120 ms superframe held the last modal frame (165 Hz, full level) and four creak frames. The
line fit went with the majority, and the outlier rule then dropped the modal frame. So 400
played 91 Hz on the vowel and a steady 94-105 Hz buzz, rising, for 120 ms. 1200 plays 165 Hz to
the end of the vowel and then 80 ms of the creak.

| 13.32-13.42 s | modal | creak | creak | creak | creak | (unvoiced) |
|---|---:|---:|---:|---:|---:|---:|
| tracker | 165 | 89 | 93 | 96 | 96 | |
| sotto 400 before | 91 | 94 | 96 | 100 | 101 | 105 |
| sotto 400 after | 157 | 138 | 119 | 103 | 89 | 79 |

Fix (`SottoVLR.creak_mask`, `CREAK_*`; `c/sotto_vlr.c` `creak_step`). Encoder only, so the
bitstream and every decoder, the K6's included, are unchanged.

- A voiced frame is a **creak frame** when it sits at least 25 index steps (about 0.65 of an
  octave) below the voiced frame just before it. That frame and the one before it must both be
  fully voiced and within 6 steps of each other. The following voiced frames stay creak while
  they're 20 or more steps below that steady pitch.
- In a superframe holding both creak and modal frames, a creak frame weighs 1 in the line fit
  and the search, against 4 for a modal frame. The outlier rule is skipped there, and the
  search tries every centre.
- So the line keeps the modal frame and falls into the creak as far as the slope table
  reaches, as a voice falls at a phrase end.
- A superframe that is all creak is coded as before.

The steadiness checks matter. Without them, an onset's junk pitch (LibriSpeech 251 at 6.38 s:
500 Hz on an onset edge, then 96 Hz) made the real voice after it look like creak, and 400
played 349 Hz there. Likewise, the mixed-voicing onset frames before true 62 Hz fry
(LibriSpeech 3081 at 10.68 s) turned the fry into a glide from 247 Hz.

On the 16 voices (`tools/pitch_errors.py --modes 600,400`; no voice got worse):

| | 600 off20 | 600 gross | 400 off20 | 400 gross |
|---|---:|---:|---:|---:|
| before | 285 | 157 | 296 | 141 |
| after | 270 | 151 | 281 | 137 |

The gains come from EARS p020 and p070, whose voices go creaky or get halved by the tracker mid-run.
PESQ-NB is flat (600 2.1328 → 2.1323, 400 1.8609 → 1.8669; ESTOI 0.692 / 0.641 both). Only
three voices change at all, so this is a rare event that is loud when it happens.

The C encoder matches the Python.

- `tools/check_vlr_hiss.py`, on the 16 voices plus LibriSpeech 198: every superframe matches,
  2,422 of 2,422 at 600 and 1,616 of 1,616 at 400.
- `tools/check_vlr.py`: 99.92% and 99.94%. The only misses are the same EARS p051 frames as
  before the change, which come from the fixed-lag tracker.
- C encoder into C decoder, PESQ-NB: 600 2.114 → 2.118, 400 1.842 → 1.857.

`docs/sotto.wasm` was rebuilt, since the page's 600 / 400 encoders are this C code. LibriSpeech
198's 400 stream changes in one superframe, the one holding "my".

## Still open

**The tracker halves some female voices in breathy or rough stretches.** About 140 tracker
frames are at half or a third of pYIN's pitch:

| Voice | Time | Tracker | pYIN |
|---|---|---|---|
| EARS p070 | 0.82-0.94 s | 166 Hz | 334 Hz |
| EARS p070 | 8.06-8.19 s | 85 Hz | 171 Hz |
| EARS p070 | 8.44-8.54 s | 108 Hz | 214 Hz |
| EARS p051 | 6.35-6.46 s | 76-100 Hz | 227 Hz |
| EARS p051 | 3.77-3.83 s | 108 Hz | 200 Hz |
| LibriSpeech 3536 | 1.64 s | 82 Hz | 262 Hz |

Codec 2's own estimate is right in most of these. The tracker overrules it because the voice has
energy between its harmonics, and a comb at half the pitch fits that better (the MBE fit error is
1-2 units lower). The odd/even sub-octave check doesn't fire: the odd harmonics of the half pitch
are only 0-2 dB down, not the 10 dB it looks for.

An experiment, `track_pitch_voicing(up=4, up_thr=0.65)`, adds a time-domain check: a candidate
costs extra when the speech repeats at half (or a third of) its period, which means its double is
the pitch.

- Octave-low frames drop from 142 to 97, with no new octave-high ones. Per voice: p070 38 → 15
  frames off, p051 21 → 10.
- PESQ is flat: 2400 −0.016, 1200 and 600 within 0.005, 400 +0.006.
- It is off by default and has no C port. Whether it sounds better is for a listening test.

**Creaky phrase ends.** In LibriSpeech 3081 at 9.5-11 s, pYIN hears 60-65 Hz creak and sotto
plays 115-260 Hz. pYIN's floor makes this one hard to judge without ears.
