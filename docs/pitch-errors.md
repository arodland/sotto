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
