# sotto 1200 with the 4-8 kHz hiss band

*2026-11-06. Python reference `sim/sotto_lr_hiss.py` (`Sotto1200Hiss`); B-frame tables
`tools/train_lr_hiss.py`; scores `tools/eval_lr_hiss.py` → `docs/results/lr_hiss.json`;
listening round 19. Float C port in `c/sotto_hiss.c` (`sotto_lr_enc16` / `sotto_lr_dec16`),
checked by `tools/check_lr_hiss.py` → `docs/results/lr_hiss_c_check.json`.*

Jeff asked whether v3.8's hiss band (s, sh, f and t above 4 kHz, `docs/unvoiced-hiss.md`) can
go into sotto 1200 (`docs/sotto-1200.md`). It can, and it needs no extra bits: the frame stays
48 bits per 40 ms.

## Where the bits come from

v3.8 synthesises unvoiced halves as noise harmonics 50 Hz apart, so an all-unvoiced (UU)
frame's pitch field carries nothing, and v3.8 puts a 6-bit hiss index and one side-channel bit
there. sotto 1200 has the same spare bits in both of its 20 ms frames:

| Frame | Unvoiced share (16 voices) | Spare bits | Use |
|---|---:|---|---|
| A (bits 2-8, pitch A) | 22-24% | 7 | v3.8's 6-bit index into the same 64 × 4 codebook + 1 side bit |
| B (bits 34-36, B's pitch step) | 24% | 3 | 2-bit correction + 1 side bit |

B's 2 bits pick one of four level offsets (−11, −2, +3, +12 dB, LibriSpeech-trained) added to
the mean of the quantised hiss of the unvoiced A frames on either side of it. With no unvoiced
neighbour the offset is added to the decoder's guess alone. Every unvoiced frame carries a side
bit, as in v3.8, so the per-speaker vowel shape locks in as fast as it does at 2400.

Everything else is v3.8's candidate as it stands: its encoder voicing rules (bursts and
sibilants sent as UU, creaky onsets as MM, whispered runs as UU, flutter smoothing), the
decoder's +3 dB on rising unvoiced frames, the guess from the 0-4 kHz envelope, the vowel
layer and the blend. The 0-4 kHz part is sotto 1200's. The changes outside the new file are
three overridable hooks in `hiss_band.py` (`_hiss_model`, `_uu_res`) and `sotto_lr.py`
(`_synth` calls the model hook and passes the UU pitch guard). v3.8's output is bit-identical
before and after, and plain sotto 1200's hook is a no-op.

## Results

16 held-out voices at 16 kHz (the `samples-clean` cuts rebuilt from LibriSpeech dev-clean and
EARS). Hiss error: on fricative frames (input 4-8 kHz within 10 dB of its 0-4 kHz, loudest
70%), rms over the four 1 kHz bands of output dB minus input dB, measured on the decoded audio.

| Variant (bits per 40 ms) | PESQ-NB | ESTOI | PESQ-WB | hiss error rms / bias |
|---|---:|---:|---:|---:|
| sotto 1200 (48) | 2.545 | 0.764 | 1.589 | no band (−60 dB) |
| **sotto 1200 hiss, B 2 bits + side (48)** | 2.517 | 0.759 | 1.555 | **5.28 / −1.15 dB** |
| the same, B 3 bits, side bit in A only | 2.518 | 0.759 | 1.553 | 5.36 / −1.13 |
| B 3 bits, absolute (no anchors) | 2.517 | 0.759 | 1.546 | 6.01 / −1.21 |
| B from the anchors only (0 bits used) | 2.516 | 0.759 | 1.537 | 6.43 / −2.08 |
| v3.8 at 2400 (96) | 2.829 | 0.810 | 1.694 | 5.02 / −0.83 |

With one K6 XL packet in 8 lost (lone superframes, rebuilt by sotto 1200's rule; a rebuilt
unvoiced frame takes the mean of its neighbours' hiss):

| | PESQ-NB | PESQ-WB | hiss error |
|---|---:|---:|---:|
| sotto 1200 | 2.156 | 1.421 | no band |
| sotto 1200 hiss | 2.147 | 1.365 | 7.64 / −2.28 dB |

- **The band comes out at v3.8's accuracy.** Fricative hiss lands within 5.3 dB rms of the
  original, against v3.8's 5.0 at twice the rate. On the round-19 clips, the share of energy above 4.2 kHz
  is within about 2 dB of the original's (0.1-2.2 dB).
- **B's 2 bits plus a side bit beat 3 bits of hiss.** Most of B's information is already in its
  neighbours. The anchor mean alone gets 6.4 dB, and 2 bits of level fix most of the rest.
- **The 0-4 kHz cost is v3.8's own.** PESQ-NB drops 0.028 on average, from v3.8's voicing
  rules (the "z fix" cost v3.8 0.04 at 2400 too). The loss is concentrated in four voices
  (EARS p025 −0.15, p095 −0.13, p107 −0.11, LibriSpeech 777 −0.09); others gain slightly.
  The rules exist for the ears, not for PESQ: round 2 of v3.8 judged them.
- **PESQ-WB punishes added hiss** (`docs/unvoiced-hiss.md`), so it drops 0.03 here even though
  the band is right. Ears decide (round 19).

## The K6

The K6 can't play any of this:

- **Its output runs at 8 kHz.** The pager's PWM updates at 8 kHz (TIM1 at 48 kHz, RCR = 5),
  so nothing above 4 kHz exists in its output.
- **The RC network after the PWM pin cuts the band anyway.** 4.7 kΩ / 3.9 nF, 4.7 kΩ / 22 nF,
  then 22 kΩ + 1 µF into the 2.2 kΩ node (`docs/k6-receiver.md`) is, relative to 300 Hz,
  −12 dB at 4 kHz, −14 dB at 5 kHz, −16 dB at 6 kHz and −18 dB at 8 kHz, before the speaker
  amplifier and the speaker, whose response is unknown. At 16 kHz updates the zero-order hold
  adds −1 to −4 dB more.
- **16 kHz output doesn't fit.** The v3.8 16 kHz decoder costs about 2.7 times the 8 kHz one
  on the C55x (54.7 against 20.1 MHz). The K6 decodes sotto 1200 at 55 MHz mean of 96, and
  16.1 ms mean / 24 ms worst per 20 ms half while receiving. The PWM ring alone would need
  another 1,280 bytes of RAM, against about 900 free.

So for the K6, hiss is a no-op, and the useful question is whether a hiss stream harms it:

- **Sound:** a plain sotto 1200 decoder reading the hiss stream scores 2.534 against 2.545.
  The unvoiced frames' noise is then spaced by the hiss index instead of the tracker's
  equally meaningless unvoiced pitch.
- **CPU (Cortex-M0 emulator, `c/fixed/cm0/run.py --lr`, fixed-point decoder as on the K6):**
  - the mean is unchanged: p020 55.5 / 55.5 MHz, p095 57.9 / 58.1, 8842 49.8 / 50.7 (hiss /
    plain);
  - the worst frame rises 0-5 MHz: p020 73.6 / 69.0, p095 74.5 / 74.5, 8842 79.7 / 74.6.
    That is a low hiss index read as a low pitch, so many noise harmonics.
  - The ring absorbs single slow frames, but the K6 is close to its limit while receiving.

**Recommendation:** the K6 sender keeps sending plain sotto 1200 (magic 0xA9). A hiss stream
is for receivers that output 16 kHz: the browser decoder, a PC, the FTM-300D's C5517, the
MD-UV380. If one stream must serve both, the K6's fixed decoder can clamp an unvoiced frame's
spacing to the lowest pitch it already handles. That is one line, and it also caps the plain
stream's worst case. It is not done here.

## Listening round 19

https://claude.ai/artifact/WFm87n9rayHeX26mCrntpX, key
`/mnt/project-files/vocoder/listening/ab-19-key.json` (`tools/listening/render_ab19.py`).
16 kHz, original in every pair. Sotto 1200 hiss against plain sotto 1200: 5 pairs clean, 1 with
one packet in 8 lost. Sotto 1200 hiss against v3.8 at 2400: 3 pairs. One null pair.

Result (Jeff, 2026-11-06, answer string `sotto-ab-19 1:B 2:A 3:A 4:= 5:B 6:= 7:B 8:= 9:A 10:A`;
"What I heard was really good"):

| Pairs | sotto 1200 hiss wins | ties | other wins |
|---|---:|---:|---:|
| against plain sotto 1200, clean (5) | 5 | 0 | 0 |
| against plain sotto 1200, 1 packet in 8 lost (1, 2428_m) | 1 | 0 | 0 |
| against v3.8 at 2400 (3) | 0 | 2 (777_m, 3536_f) | 1 (EARS p020_m, pair 5) |
| null pair (4) | | correct | |

The hiss band is a clear win over plain sotto 1200. At half the rate it ties v3.8 on two of
three voices. It loses on p020_m, which among the v3.8 pairs is also the voice where its
PESQ-NB drops most (2.674 → 2.620).

## C port

*2026-11-06, after round 19.* `make -C c` (VARIANT=v3.8, the default) builds it into libsotto;
the API is in `c/sotto.h` under "sotto 1200 hiss". The tools take `-16 -1200`:
`sotto_enc -16 -1200 in16k.raw out.bit` and `sotto_dec -16 -1200 in.bit out16k.raw [good.u8]`
(one flag byte per superframe). Plain `sotto_dec -1200` still plays a hiss stream at 8 kHz.

**How it is built.** Nothing of v3.8 or sotto 1200 is copied; both are reused:

- **Decoder.** The sotto 1200 streaming decoder (`sotto_lr_dec.inc`) calls a hook after every
  20 ms frame its core synthesises, with the frame's voicing, its 4-band hiss correction, its
  side bit, whether its superframe arrived, and the concealment run. The correction is worked
  out in the .inc where the superframe fields are at hand: A's 6-bit index, B's 2-bit level
  over the mean of its unvoiced anchors, and for a rebuilt superframe the anchors' mean alone
  (no side bit). The hook feeds v3.8's 16 kHz layer, which now takes a correction per frame
  instead of reading an index out of a v3.8 frame (`dec_push`). The hook code exists only
  under `SOTTO_V38` in the float build, so the fixed-point decoder and the K6 images compile
  exactly as before.
- **Encoder.** v3.8's front end (decimation, the core, input features, the voicing rules over
  the ring) hands its final 20 ms frames to sotto 1200's coder, run without its own core. The
  superframes go through a local decoder (the v3.7 core on the frames the sotto 1200 decoder
  rebuilds), whose envelope gives the guess and whose output gives the side channel's
  predictor, as in v3.8. Superframe s is finished once A(s + 1) can be quantised, since B(s)
  needs it.
- Small refactors on the way, all output-neutral: `enc_side`, `enc_residual` and `nearest4`
  are shared by both encoders, and the Python decoder's concealment step became an
  overridable `_conceal_rows`. Two semantic fixes in the Python came with it: the rows
  sotto 1200 hands v3.8's layer are already concealed (they were concealed a second time
  frame by frame), and a rebuilt superframe carries no side bit. Rescored, the loss row above
  moved by 0.01 dB.

**Check against the Python** (`tools/check_lr_hiss.py`, the 16 test voices, whole clips, the
Python with the C's fixed-lag tracker and whisper look-ahead, as for v3.8):

| | clean | 1 superframe in 8 lost |
|---|---:|---:|
| Superframes whose 48 bits match | 4,485 of 4,487 (14 voices all, p020 and 251 one each) | (same bits) |
| 0-4 kHz SNR, C decoder vs Python decoder on the same bits | 26-45 dB | 27-44 dB |
| 4-8 kHz level per 10 ms, mean / 95th pct. |difference| | 0.72 / 1.84 dB | 0.73 / 1.85 dB |
| PESQ-NB: Python / C decoder / C encoder + C decoder | 2.557 / 2.556 / 2.555 | 2.156 / 2.157 / 2.157 |

- The two superframes that differ are a shape near-tie on p020 (both A stages) and an energy
  code on 251. 251 then scores 0.02 lower in PESQ; nothing else moves.
- The decoders differ only in the noise band's random phase: 0.72 / 1.84 dB is the spread
  between two Python seeds for v3.8 (0.73 / 1.92). The 0-4 kHz SNR is limited the same way
  as v3.8's (hiss near 4 kHz leaks into the measurement with its random phase).
- **Delay.** The encoder returns its first superframe on the 12th 40 ms call (sotto 1200's
  look-ahead, v3.8's 200 ms of rule look-ahead, and the local decoder waiting for
  A(s + 1)'s envelope). The decoder plays 4 superframes behind: sotto 1200's 2 plus v3.8's
  layer.
- **Cost on a PC** (11.5 s of p020): the decoder takes the same time as v3.8's 16 kHz
  decoder, the encoder about 1.5 times v3.8's (sotto 1200's shape search on top).

**Fixed point.** Not done. The fixed-point build links stubs that return NULL (the tools say
"use the float build"). The C55x would need it to play or send hiss streams on the FTM-300D;
the MD-UV380's Cortex-M4F can run the float code. The fixed v3.8 layer in
`c/fixed/sotto_fx_hiss.c` mirrors the float one, so the same hook and `dec_push` change carries
over, plus B's table in Q format.

## Open

- **Fixed-point port** for the C55x (above).
- **v3.9's hiss ceiling** (unmerged branch, `docs/hiss-ceiling.md`) rides the same side-channel
  bit, so it should stack on this unchanged once v3.9 merges.
