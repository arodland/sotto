# Very-low-rate sotto: sotto 600 and sotto 400

*2026-11-06. Python reference `sim/sotto_vlr.py` (`SottoVLR("600")`, `SottoVLR("400")`), tables
`sim/data/vlr_tables.npz` from `tools/train_vlr.py`, scores from `tools/vlr_race.py`. Float C
encoder `c/sotto_vlr.c` (in libsotto, `sotto_enc -600/-400`), fixed-point decoder
`c/fixed/sotto_fx_vlr.c`. Listening round 20 (Jeff): sotto 600 beat Codec 2 700C 3-0 and
Codec 2 1200 2-0; sotto 400 split 1-1 with 700C. Hiss band: `sim/sotto_vlr_hiss.py`, see below.*

## The short answer

Yes, with limits. Both modes are non-neural, use sotto 1200's v3.7 synthesis unchanged, and keep
the 6-byte slot:

| 16 held-out voices, PESQ-NB / ESTOI | bit/s | clean | 1 K6 XL packet in 8 lost | 10% superframes lost |
|---|---:|---:|---:|---:|
| sotto 1200 (reference) | 1200 | 2.578 / 0.762 | 2.168 / 0.715 | 2.148 / 0.713 |
| Codec 2 1200 | 1200 | 2.145 / 0.683 | | |
| **sotto 600** | **600** | **2.096 / 0.690** | **1.814 / 0.638** | 1.795 / 0.638 |
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

`c/fixed/sotto_fx_vlr.c` (its body in `c/sotto_vlr_dec.inc`, shared with the float decoder
since the hiss band's C port; API in `c/sotto_vlr.h`, tool `c/fixed/sotto_vlr_dec_fx 600|400
in.bit out.raw [good.u8]`) turns each superframe into N per-frame field sets (voicing code,
pitch index, energy code, 19 shape coefficients in dB Q16) and calls the v3.7 fixed-point
synthesis core once per 20 ms frame (`sotto_dec_frame`), as sotto 1200's decoder does. Its
rounding is integer (half up), and the Python reference rounds the same way, so the two build
the same fields for every frame, with losses too (checked frame by frame on two voices,
clean and with 1 superframe in 5 or 8 lost).

- **Against the Python decoder** (`tools/check_vlr.py`, `docs/results/vlr_fx_check_*.json`, 16
  voices): mean PESQ 2.083 against 2.095 (600, with the C tracker's fixed lag) and 1.808 against 1.813 (400); with 10% of
  superframes lost, 1.871 / 1.879 and 1.672 / 1.677. The waveforms match to 62-65 dB SNR on a
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
- **Per frame.** `sotto_vlr_decode_frame` plays one 20 ms frame per call (the anchors are worked
  out at the superframe's first frame), so the K6 pager needs no 640- or 960-sample buffer; it is
  bit-exact with the whole-superframe call (same samples, with losses too).

## The K6 image

The pager library (`c/k6`) plays sotto 600 and 400 in a build on top of sotto 1200's
(`K6_CODEC=vlr sh tools/k6_build.sh`, `-DSP_LR -DSP_VLR`; Aura images `aura-pager-vlr` and the
one-time install `aura-pager-vlri`, `firmware/k6-aura/README.md`):

- **Magic bytes.** 0xAC is sotto 600 and 0xAD sotto 400 (`modem/pager.py`; 0xAA is
  `tools/k6_update.py`'s and 0xAB the RAM modules'). Every build takes the packets (CRC, store,
  bit-error tests); only this one plays them. A change of magic starts a new message.
  `python -m modem.k6 send msg.sot --codec 600` sends them; `tools/k6_pcmhash.py --codec 600`
  checks the radio's `pcm=`.
- **Tables in SPI flash.** The 21,280 bytes of int8 shape codebooks (vlr_cb_all in
  `c/sotto_vlr_tables.c`: 6,688 for 600, 14,592 for 400) sit at SPI flash 0x108000, after v3.7's
  codebook (`c/k6/sp_vlr.h`: a 32-byte header, `K6VL`, CRC-32s; image `c/k6/vlr_image`). The
  decoder reads a 19-byte vector per anchor stage (`VLR_CB_VEC`): 6 per 80 ms or 4 per 120 ms,
  34 us each on the radio. The 0.7 KB of small tables stay in the MCU. `sp_vlr_setup` checks the
  image at power-on (serial `K6 VLR r= crc= ms=`); the install build writes it once.
- **Host checks** (`tests/test_k6_vlr.py`, `make -C c/k6`): the pager's output is bit-exact with
  `sotto_vlr_dec_fx` for L and XL, with lost packets; the SPI-flash build matches the table
  build; a missing or damaged image plays no sotto 600 / 400 but sotto 1200 still plays; the
  install writes vlr_image's bytes once; a 1500-slot message streams through the 640-slot ring as
  a store-then-play plays it.
- **Cortex-M0** (`c/k6/bench_k6.py`, the radio's -O2/-Os build, EARS p020,
  `docs/results/vlr_k6_bench_*.json`), decode plus PWM per 20 ms: sotto 600 57.9 MHz mean, 71.5
  worst; sotto 400 57.7, 76.6; sotto 1200 in the same build 57.5, 75.8 (of 96). Library 4.3 KB
  more flash than sotto 1200's and 340 bytes more RAM (decoder pool 1.5 KB).
- **Not yet on air.** The Aura images are built by CI only (no cargo here).

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
sotto 600). Jeff, 2026-11-06:

| Pairs | sotto wins | ties | other wins |
|---|---:|---:|---:|
| sotto 600 against Codec 2 700C (3) | 3 | 0 | 0 |
| sotto 600 against Codec 2 1200 (2) | 2 | 0 | 0 |
| sotto 400 against Codec 2 700C (2) | 1 (ears p025_m) | 0 | 1 (6313_f) |
| sotto 600 against sotto 400 (1) | 1 (600) | 0 | 0 |
| sotto 600 against sotto 1200, clean (1) | 0 | 1 (777_m) | 0 |
| sotto 600 against sotto 1200, 1 XL packet in 8 lost (1) | 0 | 0 | 1 (1200) |
| null pair | | correct | |

By ear sotto 600 does better than PESQ says: it beat Codec 2 700C every time, beat Codec 2 1200
at half its rate (PESQ called that level), and tied sotto 1200 on one clean clip. With packet
loss sotto 1200 is better, as expected: a lost 600 slot takes 80 ms of speech, not 40. sotto
400 splits with Codec 2 700C, matching PESQ.

## Hiss band (4-8 kHz)

*Python reference `sim/sotto_vlr_hiss.py` (`SottoVLRHiss("600")`), tables `sim/data/vlr_hiss.npz`
from `tools/train_vlr_hiss.py` (32 non-test LibriSpeech dev-clean voices, CC BY 4.0), scores
`tools/eval_vlr_hiss.py` → `docs/results/vlr_hiss.json`. Listening round 21: won 7 of 7 against the
plain modes. Float C port in `c/sotto_hiss.c` (`sotto_vlr_enc16` / `sotto_vlr_dec16`, below),
checked by `tools/check_vlr_hiss.py` → `docs/results/vlr_hiss_c_check.json`.*

Jeff asked (2026-11-06) whether v3.8's hiss band (`docs/unvoiced-hiss.md`) goes into sotto 600
and 400 as easily as into sotto 1200 (`docs/sotto-1200-hiss.md`). It fits in the same 48 bits,
but it reaches fewer frames, because the spare bits sit only in superframes without voiced
speech:

| Superframe (decoded voicing pattern) | Spare bits | Use | U frames there, 600 / 400 (plain encoder) |
|---|---|---|---:|
| no pitched frame (pattern all U) | pitch centre 7 + slope 3 | v3.8's 6-bit hiss index + 1 side bit; a level slope (8 trained steps, −11 to +8 dB per frame) | 59% / 43% |
| one pitched frame | slope 3 (one point has no slope; the pitch is the centre) | a 2-bit level offset over the neighbours' hiss + 1 side bit | 13% / 16% |
| two or more | none | the neighbours' hiss (nearest coded frame either side, within 2 superframes), or a trained mean | 28% / 41% |

v3.8's encoder rules (bursts and sibilants sent as U, whispers, creaky onsets, flutter
smoothing) change the per-20 ms voicing codes before the pattern search, and the decoder is
v3.8's (guess from the 0-4 kHz envelope, vowel layer, blend).

16 held-out voices at 16 kHz, same metrics as sotto 1200 hiss (hiss error on fricative frames):

| | PESQ-NB | ESTOI | PESQ-WB | hiss error rms / bias |
|---|---:|---:|---:|---:|
| sotto 600 | 2.072 | 0.692 | 1.390 | no band |
| **sotto 600 hiss** | 2.094 | 0.690 | 1.340 | **8.56 / −3.43 dB** |
| the same, slope kept as pitch | 2.094 | 0.690 | 1.338 | 8.85 / −3.76 |
| sotto 600 decoder reading the hiss stream | 2.091 | 0.692 | 1.391 | no band |
| sotto 400 | 1.805 | 0.638 | 1.293 | no band |
| **sotto 400 hiss** | 1.828 | 0.635 | 1.247 | **11.70 / −6.27 dB** |
| sotto 1200 hiss, for comparison | 2.517 | 0.759 | 1.555 | 5.28 / −1.15 |

With 1 XL packet in 8 lost: 600 hiss 11.38 dB, 400 hiss 15.17 dB.

- **Where the bits are, the band is as good as v3.8's.** On sotto 600, fricative frames in
  all-U superframes land within about 4.9 dB rms (bias −0.8, before the tables were trained), v3.8's own accuracy at 2400 (5.0). Next
  to voiced speech it falls apart: about 9 dB with the 2-bit offset, 14 dB from the neighbours
  alone. 12% of the fricative frames are coded voiced or mixed by the pattern search and get
  the vowel layer instead (16-32 dB short), which no table fixes.
- **The negative bias is selection.** A frame with no bits gets its neighbours' level, and the
  fricative frames measured are the loud ones, so it comes out too quiet. The trained mean
  over the neighbours is close to 0 dB.
- **The side channel never locks.** One side bit per carrying superframe gives 25 (600) or
  at most 16 (400) bits per 2 s window; the 16-bit message needs 16 carrying superframes in a
  window, which speech rarely has. Without it the score is identical, so the decoder runs on
  the mean speaker shape.
- **v3.8's voicing rules help the 8 kHz part a little:** a plain sotto 600 decoder reading the
  hiss stream scores 2.091 against 2.072 for plain sotto 600 (400: 1.830 against 1.805).
  The one-pitched-frame superframes lose their slope there, but that costs nothing measurable.
- **The K6 is unchanged:** it plays 8 kHz, so it can take a hiss stream and ignore the band, as
  with sotto 1200. Its decoder would need `sf_pitch`'s zero-slope rule for one-pitched-frame
  superframes (in `c/sotto_vlr_dec.inc` since the C port, but only the float hiss decoder
  switches it on).

So the hiss band in sotto 600 is roughly sotto 1200 hiss's "anchors only" variant (6.4 dB)
or worse, and sotto 400's is weaker again. Round 19 found sotto 1200's band clearly worth having
(it won all 6 pairs), so round 21 asks the ears about this one
(`tools/listening/render_ab21.py`, key `/mnt/project-files/vocoder/listening/ab-21-key.json`,
https://claude.ai/artifact/4A7QkE4JwYJ5YgbMrqp7wy): sotto 600 hiss against plain sotto 600
(4 pairs clean, 1 with 1 XL packet in 8 lost), sotto 400 hiss against plain sotto 400 (2),
sotto 600 hiss against sotto 1200 hiss (2), and a null pair. Jeff, 2026-11-06:

| Pairs | hiss wins | ties | other wins |
|---|---:|---:|---:|
| sotto 600 hiss against sotto 600, clean (4) | 4 | 0 | 0 |
| sotto 600 hiss against sotto 600, 1 XL packet in 8 lost (1) | 1 | 0 | 0 |
| sotto 400 hiss against sotto 400 (2) | 2 | 0 | 0 |
| sotto 600 hiss against sotto 1200 hiss (2) | | 2 (777_m, p047_f) | |
| null pair | | correct | |

By ear the band is worth having at both rates, despite the larger band error: it won all 7
pairs against the plain modes, and sotto 600 hiss tied sotto 1200 hiss on both clips.

### C port

*2026-11-07, after round 21 (Jeff: "Go ahead and build the C ports of the hiss band").*
`make -C c` (VARIANT=v3.8, the default) builds it into libsotto; the API is in
`c/sotto_vlr.h` under "sotto 600 / 400 hiss". The tools take `-16 -600` or `-16 -400`:
`sotto_enc -16 -600 in16k.raw out.bit` and `sotto_dec -16 -600 in.bit out16k.raw [good.u8]`
(one flag byte per superframe). A plain sotto 600 / 400 decoder (the K6's) still plays a hiss
stream at 8 kHz.

It is built the way sotto 1200 hiss is (`docs/sotto-1200-hiss.md`, "C port"):

- **One decoder source for the K6 and the PC.** The fixed-point decoder's body moved into
  `c/sotto_vlr_dec.inc`. `c/fixed/sotto_fx_vlr.c` (the K6 pager, the browser page's
  `sotto_vlr.wasm`) includes it with shapes in dB Q16, as before, and decodes sample for sample
  as before (checked on hts1a at both rates, with losses; the K6 tests pass). The new `c/sotto_vlr_dec.c` includes it on the float v3.7 core (shapes in dB), so the
  hiss decoder doesn't need a second copy of the stream logic (loss rebuild, anchor
  interpolation, frame-at-a-time play).
- **Decoder.** In the float v3.8 build the .inc calls a hook after every 20 ms frame its core
  synthesises (frame number, voicing code, concealment run, whether the superframe arrived
  or was rebuilt), and with a hook set it reads a one-pitched-frame superframe's pitch as
  flat (`sf_pitch`). The hook in `sotto_hiss.c` keeps the hiss fields of the last 8
  superframes received, works out the frame's 4-band correction (index plus slope; the
  neighbours' hiss plus the 2-bit offset or `k2`; `h0`) and whether it carries the
  superframe's side bit, and feeds v3.8's 16 kHz layer (`dec_push`, which now takes a
  "carrier" flag instead of assuming every unvoiced frame carries one). The neighbours two
  superframes ahead are always in, because the decoder plays superframe s once s + 2 has
  arrived.
- **Encoder.** v3.8's front end (decimation, core, input features, voicing rules) hands its
  final 20 ms frames to the sotto 600 / 400 coder in `c/sotto_vlr.c`, run without its own core
  (`sotto_vlr_enc_create_frames`, `_push`, `_end`). Each superframe that comes out gets the
  one-pitched-frame pitch fix and goes through a local float sotto 600 / 400 decoder, whose
  envelope gives the guess and whose output the side channel's predictor. Stage 1 picks a
  kind-0 superframe's index and slope (joint search) as soon as its envelope is in; stage 2
  finishes superframe s once stage 1 has reached s + 2: the kind-1 offset over the
  neighbours, the side bit, the fields packed.
- **The Python** got the two fixes sotto 1200 hiss's port brought there: the rows handed to
  v3.8's layer are already concealed (`_conceal_rows`), and a rebuilt superframe carries no
  side bit. Clean scores don't move; the loss rows above are rescored with them.

**Check against the Python** (`tools/check_vlr_hiss.py`, the 16 test voices, whole clips, the
Python with the C's fixed-lag tracker and whisper look-ahead):

| | sotto 600 hiss | sotto 400 hiss |
|---|---:|---:|
| Superframes whose 48 bits match | 2,243 of 2,248 (14 voices all) | 1,499 of 1,500 (15 voices all) |
| 0-4 kHz SNR, C decoder vs Python decoder on the same bits | 26-44 dB | 28-42 dB |
| 4-8 kHz level per 10 ms, mean / 95th pct. \|difference\| | 0.73 / 1.84 dB | 0.72 / 1.82 dB |
| PESQ-NB clean: Python / C decoder / C encoder + C decoder | 2.094 / 2.095 / 2.092 | 1.814 / 1.814 / 1.815 |
| the same, 1 superframe in 8 lost | 1.804 / 1.804 / 1.802 | 1.612 / 1.612 / 1.611 |
| the same, 10% random loss | 1.877 / 1.876 / 1.876 | 1.676 / 1.670 / 1.671 |

- The superframes that differ: on p095 (600) shape anchors at near-ties, as in the plain
  coder (`tools/check_vlr.py`); on p051 (one superframe at each rate, and the one after it at
  600) the core's pitch tracker, which takes a near-tie octave the other way on the C's
  16-bit decimated input (as in v3.8's own check).
- The decoders differ only in the noise band's random phase (0.73 / 1.84 dB is the spread
  between two seeds) and the 0-4 kHz synthesis rounding, as for sotto 1200 hiss.
- **Delay.** The encoder writes a superframe about 10 superframes (sotto 600, 800 ms) or 9
  (sotto 400, 1.08 s) after its speech: the core's look-ahead and v3.8's rules (16 frames),
  the anchor search, the local decoder's 2 superframes, and stage 2 waiting for the hiss 2
  superframes ahead. The decoder plays `SOTTO_VLR_DEC16_DELAY` superframes behind: 4 at 600,
  3 at 400 (sotto 600 / 400's 2 plus v3.8's layer, rounded up to whole superframes). For the
  store-and-forward pager neither matters.
- **Cost on a PC** (11.5 s of p020): the encoder takes about v3.8's 2400 encoder's time (0.14
  s), the decoder a little less than v3.8's 16 kHz decoder.
- **Fixed point.** Not done (stubs return NULL, as for sotto 1200 hiss). The K6 keeps playing
  8 kHz. If hiss streams become what we send it, it could switch on the .inc's zero-slope rule
  for one-pitched-frame superframes (`sf_pitch`, now tied to the hook).

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
make -C c                                            # VARIANT=v3.8: the hiss band
c/sotto_enc -16 -600 in16k.raw out.bit               # sotto 600 hiss: 16 kHz in
c/sotto_dec -16 -600 out.bit back16k.raw [good.u8]
python tools/check_vlr_hiss.py CLIP16K.wav ... [--every 8 | --loss 0.1] [--json OUT]
```

```python
import sotto_vlr as vr
v = vr.SottoVLR("600")                # or "400"
bits = v.encode(x)                    # x: 8 kHz float speech; (superframes, 48) bits
y = v.decode_audio(bits, good=None)   # good: per-superframe flags
```

## Open

- On air: flash `aura-pager-vlri`, then send a sotto 600 and a sotto 400 message and check `pcm=`.
- References that need an outside build: Codec 2 450, MELPe 600.
- Hiss band: a fixed-point port (C55x, and the K6 if it should play 16 kHz some day), and the
  browser page's float `sotto.wasm` could export the 16 kHz pair. To reach more fricatives, the voicing
  pattern search could weight frames that v3.8's rules sent U more heavily, so fewer of them
  land in voiced patterns.
- Not tried: a residual for the interpolated frames (as sotto 1200's B frame), which needs bits
  taken from the anchors; variable anchor positions.
