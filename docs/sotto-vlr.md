# Very-low-rate sotto: sotto 600 and sotto 400

*2026-11-06. Python reference `sim/sotto_vlr.py` (`SottoVLR("600")`, `SottoVLR("400")`), tables
`sim/data/vlr_tables.npz` from `tools/train_vlr.py`, scores from `tools/vlr_race.py`. Float C
encoder `c/sotto_vlr.c` (in libsotto, `sotto_enc -600/-400`), fixed-point decoder
`c/fixed/sotto_fx_vlr.c`. Listening round 20: see below.*

## The short answer

Yes, with limits. Both modes are non-neural, use sotto 1200's v3.7 synthesis unchanged, and keep
the 6-byte slot:

| 16 held-out voices, PESQ-NB / ESTOI | bit/s | clean | 1 K6 XL packet in 8 lost | 10% superframes lost |
|---|---:|---:|---:|---:|
| sotto 1200 (reference) | 1200 | 2.578 / 0.762 | 2.168 / 0.715 | 2.148 / 0.713 |
| Codec 2 1200 | 1200 | 2.145 / 0.683 | | |
| **sotto 600** | **600** | **2.097 / 0.690** | **1.814 / 0.638** | 1.795 / 0.638 |
| Codec 2 700C | 700 | 1.909 / 0.606 | | |
| **sotto 400** | **400** | **1.826 / 0.637** | 1.609 / 0.577 | 1.645 / 0.587 |

- **sotto 600** beats Codec 2 700C on 15 of 16 voices by PESQ and on all 16 by ESTOI, at 6/7 of
  its rate. Against Codec 2 1200, at half the rate, it is level: PESQ 0.05 lower, ESTOI 0.01
  higher (it wins 6 and 10 of 16).
- **sotto 400** is level with Codec 2 700C: PESQ 0.08 lower (wins 5 of 16), ESTOI 0.03 higher
  (wins 14 of 16), at 4/7 of the rate. That is about as far as this structure goes.
- Neither is close to sotto 1200. Going from 1200 to 600 costs 0.5 PESQ and 0.07 ESTOI.
- **The K6 can decode both.** On the Cortex-M0 emulator the fixed-point decoder takes 56 MHz
  on average and 66 (600) or 71 (400) MHz in the worst superframe, of the K6's 96, the same
  as sotto 1200 (55 and 67).
- Not measured: Codec 2 450 and MELPe 600. Our Codec 2 fork is 1.2, which dropped mode 450,
  and this sandbox refuses to build code downloaded from outside (an older Codec 2, or the
  STANAG reference MELPe code, which is reference-only anyway). Comparing against them needs
  a run on a machine that can build them.

## What it is for

On the K6 pager (FFSK 1800, XL packets of 20 slots), a slot now carries 80 ms (600) or 120 ms
(400). From `modem.pager.Air.seconds`, 16-byte first preamble, the later preambles as shown:

| Air time at F/1800 XL | 12 s message | 60 s message |
|---|---:|---:|
| v3.7 (2400), 24-byte later preambles | 22.7 s | 105.9 s |
| sotto 1200, 24-byte | 11.6 s | 55.9 s |
| **sotto 600**, 24-byte | **6.0 s** | **28.2 s** |
| **sotto 400**, 24-byte | 6.0 s | 22.7 s |
| sotto 600, 12-byte | 5.6 s | 26.1 s |

Short messages gain nothing from 400 over 600, because the last 160-slot interleave group is
padded (a 12 s message is one group either way). The K6's 640-slot store would hold 51 s (600)
or 77 s (400).

For the planned weak-signal mode (16 tones at 100 baud on SSB, 400 bit/s before coding):
sotto 400 fills it exactly, with nothing left for error correction or sync. With rate-1/2
coding the channel carries 200 bit/s, so speech can't go out in real time. Store-and-forward
works: a 12 s message is 600 bytes, about 24 s on air at rate 1/2 plus sync. sotto 600 at
rate 1/2 would need a 1200 bit/s channel.

## The frame

A superframe holds N 20 ms frames (N = 4 for 600, 6 for 400), coded together:

| Field | 600 | 400 | |
|---|---:|---:|---|
| voicing pattern | 3 | 3 | one of 8 trained patterns of N v3.2 voicing codes |
| pitch: centre | 7 | 7 | a least-squares line through the voiced frames' 7-bit log-Wo indices, its value at the superframe's centre |
| pitch: slope | 3 | 3 | the line's slope, from a trained table (about -7.8 to +7.8 index steps per frame) |
| energy: mean | 5 | 5 | mean of the N 5-bit energy codes |
| energy: residual | 5 | 5 | a trained N-dimensional codebook for the codes around that mean |
| shape anchors | frame 0: 7+6, frame 2: 7+5 | frames 0, 2, 4: 9, 8, 8 | trained multistage codebooks on DCT coefficients 1..19 of the mean-removed rate-K envelope |

- **Every frame between two anchors** has its shape on the straight line between them (the
  last frames run to the next superframe's first anchor). The streaming decoder therefore
  plays superframe s once s + 2 has arrived (s + 2's first anchor is the next one when s + 1 is
  rebuilt): 160 ms (600) or 240 ms (400) behind, which a store-and-forward link doesn't notice.
- **The encoder picks the anchors jointly.** A Viterbi search runs over each anchor's 16 best
  codebook paths and counts the interpolated frames' errors on both sides. So an anchor is
  chosen for the trajectory around it, not only for its own frame.
- **The tables are trained on 800 Speech Commands speakers** (CC BY 4.0, half above and half
  below 165 Hz, the round-12 speakers excluded): `tools/vlr_corpus.py` keeps each speaker's
  whole frame sequence, and `tools/train_vlr.py` trains the voicing patterns (Lloyd iterations
  under a voicing-mismatch cost), the slopes (1-D k-means), the energy residuals (k-means), and
  the shape codebooks (k-means, then 4 closed-loop passes that refit every codeword by least
  squares through the interpolation the decoder applies: -1.5% weighted error). `vlr_tables.npz`
  is 105 KB (float32, compressed). The decoder stores the shape codebooks as int8 with one
  step per stage, the slopes and energy residuals in 1/16 steps (`vlr_tables.quantize_tables`,
  which the Python reference applies too: PESQ 2.094 -> 2.097). In C
  (`c/sotto_vlr_tables.c`, from `tools/export_vlr_c.py`) that is 7.2 KB for sotto 600 and 15 KB
  for sotto 400 (whose two 8-bit slots share one codebook).
- **Lost superframes.** A lost superframe between two good ones is rebuilt from both, as in
  sotto 1200. Its shape follows the line between the neighbouring anchors, its pitch and energy
  run linearly from the frame before to the frame after, and its voicing copies the nearer
  neighbour. Longer runs hold and fade as in v3.5. On one EARS clip, rebuilding instead of
  hold-and-fade raised PESQ from 1.40 to 1.83 with 1 XL packet in 8 lost.

## How the bits were split

Oracle runs on the 16 voices, unquantized except where named (`v3.7` quantization of pitch and
energy; frames at 20 ms):

| Simplification | PESQ | ESTOI |
|---|---:|---:|
| unquantized shape, v3.7 pitch and energy | 3.040 | 0.841 |
| shape only every 40 ms, linear between | 2.657 | 0.784 |
| shape only every 60 ms | 2.268 | 0.715 |
| shape only every 80 ms | 1.970 | 0.641 |
| energy only every 80 ms | 2.568 | 0.765 |
| pitch: a line per 80 ms through the voiced frames | 2.752 | 0.832 |
| pitch: one value per 80 ms | 2.288 | 0.817 |
| pitch: linear between 80 ms points (not voicing-aware) | 2.274 | 0.811 |

- **The shape needs a point every 40 ms.** Below that, intelligibility (ESTOI) falls fast. So
  both modes send three or two anchors every 80 or 120 ms, not one.
- **PESQ is very sensitive to the pitch contour, ESTOI hardly at all.** A mean pitch per 80 ms
  costs 0.75 PESQ and only 0.02 ESTOI. A line (centre and slope) keeps most of it for 10 bits.
  Listeners probably mind less than PESQ, so these modes don't spend more there.

Then, with everything quantized (first trained tables, sotto 600 / sotto 400), each field was
put back to its true value in turn:

| | 600 PESQ / ESTOI | 400 PESQ / ESTOI |
|---|---:|---:|
| all quantized | 2.090 / 0.688 | 1.811 / 0.623 |
| true voicing | 2.082 / 0.687 | 1.797 / 0.624 |
| true energy | 2.110 / 0.696 | 1.854 / 0.638 |
| true pitch | 2.307 / 0.698 | 2.060 / 0.643 |
| true shape | 2.616 / 0.817 | 2.341 / 0.797 |

The shape is nearly all of the loss, half from quantizing the anchors and half from
interpolating between them (true anchors with interpolation: 2.341 / 0.762 for 600). The
voicing table costs nothing, so the final layout gives it 3 bits instead of 4 and the spare
bit to the shape.

What didn't help (each within ±0.03 PESQ of the others, which is noise here):

- Moving bits between pitch slope (3 or 4), energy (4+5, 5+4, 5+5, 5+6), voicing (2 to 5) and
  shape, or splitting a 12-bit anchor as 8+4, 7+5 or 6+6.
- **Predictive anchors**, coded relative to α times the previous decoded anchor (α 0.6 and 0.8,
  across superframes or reset at each one). Shapes 40 ms apart correlate at about 0.87, but the
  trained codebooks already capture that: 2.085-2.106 PESQ against 2.104 without prediction.
  The memoryless version is also the safer one when packets are lost.
- For sotto 400, three single-stage anchors every 40 ms (9, 8, 8 bits) beat two two-stage
  anchors every 60 ms (6+5 each) by 0.03 PESQ and 0.01 ESTOI.

## C encoder

`c/sotto_vlr.c` (API in `c/sotto_vlr.h`, in libsotto; `c/sotto_enc -600 | -400 IN.raw
OUT.bit`) is the float port of `SottoVLR.encode_params` on the v3.7 encoder core, as sotto
1200's `c/sotto_lr.c` is. Voicing pattern, pitch line and energy depend on a superframe's own
frames. The shape anchors come from the same Viterbi search over each anchor's 16 best paths,
run as a stream: an anchor is final once every surviving path goes through the same candidate,
which is the choice the whole-utterance search makes. (After 96 anchors without agreement the
best path would decide; that hasn't happened on speech.) The encoder therefore hands out a
superframe 145 ms after its end on average and 390 ms at most (16 voices), on top of the
core's own look-ahead.

- **Against the Python encoder** (`tools/check_vlr.py`, with the C core's fixed-lag pitch
  tracker, `TRACK2["lag"] = 8`): 99.96% of sotto 600 superframes and 99.93% of sotto 400's are
  bit-exact on the 16 voices (one superframe each differs, on EARS p051, both at frames
  336-339). C encoder into C decoder scores PESQ 2.083 (600) and 1.810 (400).
- To get there the Python pitch line now solves its normal equations in closed form, as C
  does: `lstsq` put some centres a hair below .5 and rounded them the other way.
- The fixed-point build (`make fixed`) has no encoder for these modes (`sotto_vlr_enc_create`
  returns NULL), as for sotto 1200: the K6 only receives.

## Fixed-point decoder and the K6 budget

`c/fixed/sotto_fx_vlr.c` (API in `c/sotto_vlr.h`, tool `c/fixed/sotto_vlr_dec_fx 600|400
in.bit out.raw [good.u8]`) turns each superframe into N per-frame field sets (voicing code,
pitch index, energy code, 19 shape coefficients in dB Q16) and calls the v3.7 fixed-point
synthesis core once per 20 ms frame (`sotto_dec_frame`), as sotto 1200's decoder does. Its
rounding is integer (half up), and the Python reference rounds the same way, so the two build
the same fields for every frame, with losses too (checked frame by frame on two voices,
clean and with 1 superframe in 5 or 8 lost).

- **Against the Python decoder** (`tools/check_vlr.py`, `docs/results/vlr_fx_check_*.json`, 16
  voices): mean PESQ 2.083 against 2.097 (600) and 1.822 against 1.826 (400); with 10% of
  superframes lost, 1.869 / 1.877 and 1.664 / 1.667. The waveforms match to 62-65 dB SNR on a
  few voices and drift in phase on the others (6-33 dB), as sotto 1200's fixed decoder does on
  the same voices (11.8 dB on 652_m): that is the v3.7 core, not this layer.
- **Cortex-M0 emulator** (`VLR=1 sh tools/cm0_build.sh`, `c/fixed/cm0/run.py ELF BITS --vlr 600`),
  -O2, EARS p020, `docs/results/vlr_cm0_*.json`:

  | | mean | p99 | worst superframe | ms per 20 ms at 96 MHz |
  |---|---:|---:|---:|---:|
  | sotto 600 | 56.3 MHz | 63.0 | 65.9 | 11.7 mean, 13.7 worst |
  | sotto 400 | 56.1 MHz | 64.6 | 70.6 | 11.7 mean, 14.7 worst |
  | sotto 1200 (docs/sotto-1200.md) | 55.0 MHz | 65.5 | 66.9 | 11.5 mean, 13.9 worst |

  Output is bit-exact with x86. Heap 1,072 bytes, stack 3.3 KB, both the core's. The layer is
  2.1 KB of code at -Os. It reads 4 codebook vectors per 80 ms (600) or 3 per 120 ms (400),
  against sotto 1200's 3 per 40 ms, so SPI-flash tables would cost less than they do now.
- **Not done:** the K6 image. The pager would need magic bytes for the two modes, and the
  tables in SPI flash (7.2 or 15 KB, a second one-time install next to v3.7's codebook), or a
  600-only image with them in MCU flash, which doesn't fit beside the v3.7 table.

## Listening round 20

Blind A/B with the original in every pair (`tools/listening/render_ab20.py`, key
`/mnt/project-files/vocoder/listening/ab-20-key.json`), page
https://claude.ai/artifact/HsbfSvv453Dk2hMTywm1v7:

- sotto 600 against Codec 2 700C (3 pairs) and Codec 2 1200 (2)
- sotto 400 against Codec 2 700C (2)
- sotto 600 against sotto 1200 (1) and sotto 400 (1)
- sotto 600 against sotto 1200, both with 1 XL packet in 8 lost (1)
- a null pair

The page was rendered before the tables' int8 rounding (2.094 rather than 2.097 PESQ for
sotto 600). Results: pending.

## Use

```sh
python tools/vlr_corpus.py SC_DIR corpus.npz --exclude-clips /mnt/project-files/vocoder/samples-sc
python tools/train_vlr.py corpus.npz                 # writes sim/data/vlr_tables.npz
python tools/vlr_race.py [--every 8 | --loss 0.1] [--json OUT]
python tools/export_vlr_c.py                         # c/sotto_vlr_tables.c (make tables does it too)
make -C c VARIANT=v3.7 all fixed                     # c/sotto_enc, c/fixed/sotto_vlr_dec_fx
c/sotto_enc -600 in.raw out.bit                      # 8 kHz 16-bit mono in, 6 bytes per 80 ms out
c/fixed/sotto_vlr_dec_fx 600 out.bit back.raw
python tools/check_vlr.py CLIP.flac ... --mode 600 [--loss 0.1] [--bits-dir DIR]
```

```python
import sotto_vlr as vr
v = vr.SottoVLR("600")                # or "400"
bits = v.encode(x)                    # x: 8 kHz float speech; (superframes, 48) bits
y = v.decode_audio(bits, good=None)   # good: per-superframe flags
```

## Open

- The K6 image.
- Ears: round 20.
- References that need an outside build: Codec 2 450, MELPe 600.
- Not tried: a residual for the interpolated frames (as sotto 1200's B frame), which needs bits
  taken from the anchors; variable anchor positions; the 4-8 kHz hiss band.
