# Very-low-rate sotto: sotto 600 and sotto 400

*2026-11-06. Python reference `sim/sotto_vlr.py` (`SottoVLR("600")`, `SottoVLR("400")`), tables
`sim/data/vlr_tables.npz` from `tools/train_vlr.py`, scores from `tools/vlr_race.py`. No C port
yet. Listening round 20: see below.*

## The short answer

Yes, with limits. Both modes are non-neural, use sotto 1200's v3.7 synthesis unchanged, and keep
the 6-byte slot:

| 16 held-out voices, PESQ-NB / ESTOI | bit/s | clean | 1 K6 XL packet in 8 lost | 10% superframes lost |
|---|---:|---:|---:|---:|
| sotto 1200 (reference) | 1200 | 2.578 / 0.762 | 2.168 / 0.715 | 2.148 / 0.713 |
| Codec 2 1200 | 1200 | 2.145 / 0.683 | | |
| **sotto 600** | **600** | **2.094 / 0.692** | **1.804 / 0.639** | 1.803 / 0.640 |
| Codec 2 700C | 700 | 1.909 / 0.606 | | |
| **sotto 400** | **400** | **1.825 / 0.637** | 1.597 / 0.577 | 1.656 / 0.588 |

- **sotto 600** beats Codec 2 700C on 15 of 16 voices by PESQ and on all 16 by ESTOI, at 6/7 of
  its rate. Against Codec 2 1200, at half the rate, it is level: PESQ 0.05 lower, ESTOI 0.01
  higher (it wins 5 and 10 of 16).
- **sotto 400** is level with Codec 2 700C: PESQ 0.08 lower (wins 5 of 16), ESTOI 0.03 higher
  (wins 13 of 16), at 4/7 of the rate. That is about as far as this structure goes.
- Neither is close to sotto 1200. Going from 1200 to 600 costs 0.5 PESQ and 0.07 ESTOI.
- Not measured: Codec 2 450 and MELPe 600. Our Codec 2 fork is 1.2, which dropped mode 450,
  and this sandbox refuses to build code downloaded from outside (an older Codec 2, or the
  STANAG reference MELPe code, which is reference-only anyway). Published listening tests put
  MELPe 600 near or above Codec 2 700C, so sotto 600 probably ties it rather than beating it
  clearly. That needs a run on a machine that can build them.

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
  last frames run to the next superframe's first anchor). The decoder therefore runs one
  superframe behind, which a store-and-forward link doesn't notice.
- **The encoder picks the anchors jointly.** A Viterbi search runs over each anchor's 16 best
  codebook paths and counts the interpolated frames' errors on both sides. So an anchor is
  chosen for the trajectory around it, not only for its own frame.
- **The tables are trained on 800 Speech Commands speakers** (CC BY 4.0, half above and half
  below 165 Hz, the round-12 speakers excluded): `tools/vlr_corpus.py` keeps each speaker's
  whole frame sequence, and `tools/train_vlr.py` trains the voicing patterns (Lloyd iterations
  under a voicing-mismatch cost), the slopes (1-D k-means), the energy residuals (k-means), and
  the shape codebooks (k-means, then 4 closed-loop passes that refit every codeword by least
  squares through the interpolation the decoder applies: -1.5% weighted error). `vlr_tables.npz`
  is 105 KB (float32, compressed). In int8 the shape codebooks would be about 7 KB (600) and 15 KB (400, whose two 8-bit slots share one codebook),
  small enough for the K6's SPI flash next to v3.7's codebook.
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

## K6 decode budget

The decoder turns each superframe into N per-frame field sets (voicing code, pitch index,
energy code, 19 shape coefficients) and calls the v3.7 synthesis core once per 20 ms frame,
exactly as sotto 1200's decoder does (`sotto_dec_frame` in `c/fixed/sotto_fx_dec.c`). The
extra work per frame is one 19-coefficient interpolation and a few table reads. It reads
fewer codebook vectors from SPI flash than sotto 1200: 4 per 80 ms (600) or 3 per 120 ms
(400), against 3 per 40 ms. So the expected cost on the M0 is sotto 1200's, 55 MHz mean and
67 MHz worst of 96. That is an estimate from the shared core: the fixed-point port and an
emulator run come next.

## Listening round 20

Blind A/B with the original in every pair (`tools/listening/render_ab20.py`, key
`/mnt/project-files/vocoder/listening/ab-20-key.json`), page
https://claude.ai/artifact/HsbfSvv453Dk2hMTywm1v7:

- sotto 600 against Codec 2 700C (3 pairs) and Codec 2 1200 (2)
- sotto 400 against Codec 2 700C (2)
- sotto 600 against sotto 1200 (1) and sotto 400 (1)
- sotto 600 against sotto 1200, both with 1 XL packet in 8 lost (1)
- a null pair

Results: pending.

## Use

```sh
python tools/vlr_corpus.py SC_DIR corpus.npz --exclude-clips /mnt/project-files/vocoder/samples-sc
python tools/train_vlr.py corpus.npz                 # writes sim/data/vlr_tables.npz
python tools/vlr_race.py [--every 8 | --loss 0.1] [--json OUT]
```

```python
import sotto_vlr as vr
v = vr.SottoVLR("600")                # or "400"
bits = v.encode(x)                    # x: 8 kHz float speech; (superframes, 48) bits
y = v.decode_audio(bits, good=None)   # good: per-superframe flags
```

## Open

- C port: float encoder and decoder, fixed-point decoder on the v3.7 core, int8 shape tables,
  the K6 SPI table, Cortex-M0 emulator run.
- Ears: round 20.
- References that need an outside build: Codec 2 450, MELPe 600.
- Not tried: a residual for the interpolated frames (as sotto 1200's B frame), which needs bits
  taken from the anchors; variable anchor positions; the 4-8 kHz hiss band.
