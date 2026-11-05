# sotto 1200: 48 bits per 40 ms for the UV-K6 pager

*2026-11-05. Python reference `sim/sotto_lr.py` (`Sotto1200`), float C `c/sotto_lr.c`,
fixed-point decoder `c/fixed/sotto_fx_lr.c`. Listening round 18: see below.*

## Why

The K6 pager's link is FFSK at 1800 baud. 2400 baud isn't reliable on the FD6818. A 2400 bit/s
sotto frame stream can't keep up with that link once the packet overhead is added. Each XL
packet costs 16 or 12 bytes of 0x55 preamble, a 4-byte sync and 128 bytes of payload, and 120
of those bytes are speech. sotto 1200 keeps the 6-byte slot, but each slot now carries 40 ms
instead of 20 ms, so one XL packet holds 800 ms of speech:

| At F/1800, XL packet mode | Per packet on air | 12 s message | 60 s message |
|---|---:|---:|---:|
| v3.7 (2400 bit/s), 24-byte later preambles | 693 ms per 400 ms of speech | 22.7 s | 105.9 s |
| **sotto 1200**, 24-byte later preambles | 693 ms per 800 ms | **11.6 s** | **55.9 s** |
| **sotto 1200**, 12-byte later preambles (the K6 thread's new default at 1800) | 640 ms per 800 ms | 10.8 s | 51.7 s |

These times include the 150 ms lead-in, the 16-byte first preamble, the 50 ms extra before
packet 2, the 300 ms tail, and padding of the last 160-slot group (`modem.pager.Air.seconds`).
The K6's 640-slot store now holds 25.6 s.

## The frame

A superframe covers two 20 ms frames, A and B. A is a v3.7 frame with a shorter shape. B is
coded relative to the point halfway between this superframe's A and the next one's.

| Bits | Field | |
|---|---|---|
| 0-1 | voicing A | v3.2 code (UU / VV / MM / edge), as v3.7 |
| 2-8 | pitch A | 7-bit log Wo, as v3.7 |
| 9-13 | energy A | 5-bit code, as v3.7 (0 = silence) |
| 14-31 | shape A | stages 1-2 of v3.7's 9+9+8+8 codebook (18 bits) |
| 32-33 | voicing B | v3.2 code |
| 34-36 | pitch B | step from the interpolated pitch index: -12 -5 -2 -1 0 2 5 12; if neither anchor is voiced, an absolute index |
| 37-39 | energy B | step from the interpolated energy code: -6 -3 -2 -1 0 1 2 4 |
| 40-47 | shape B | the anchors' mean shape + 1.5 x one stage-3 codeword (8 bits) |

- **The first 14 bits are a v3.7 frame's.** The K6's automatic gain reads the energy code at
  bits 9-13 and works unchanged.
- **The decoder needs no new tables.** It uses v3.7's codebook stages 1-3, three small step
  tables and the v3.7 decoder core.
- **The encoder chooses A's shape jointly.** It picks among A's 8 best codebook paths,
  counting the error of the B frames on both sides of A once each B has its best residual.
  This adds +0.06 PESQ. It makes the encoder look 2 superframes (80 ms) further ahead.
- **Losses.** The K6 plays from store, so the decoder looks one superframe further ahead
  (output is 2 superframes, 80 ms, behind). A lost superframe between two good ones is
  rebuilt from both by B's own rule with no steps or residual: its A halfway between the
  neighbouring A frames, its B halfway between that and the next A. A longer run of losses
  is two lost 20 ms frames per superframe under v3.5's concealment: it holds 40 ms, fades
  6 dB per 20 ms, and goes silent after 80 ms. XL packets interleave their slots 8 ways, so
  one lost packet in 8 loses lone superframes, which this rebuilds:

  | 16 voices, PESQ / ESTOI | hold and fade | rebuilt from both |
  |---|---:|---:|
  | 1 XL packet in 8 lost | 1.935 / 0.697 | **2.168 / 0.715** |
  | 10% of superframes lost at random | 1.928 / 0.698 | **2.099 / 0.712** |

  A linear rebuild of runs of up to 2 scores 2.182 and 2.17, but costs 930 bytes of K6
  flash against this one's 157.

## How the bits were split

The test set was 16 held-out clean voices (`/mnt/project-files/vocoder/samples-clean/`), with
mean PESQ-NB / ESTOI and the whole-utterance tracker. Every row except the 49-bit ones is
48 bits.

| Variant | PESQ | ESTOI |
|---|---:|---:|
| v3.7, 2400 bit/s (reference) | 2.937 | 0.817 |
| Codec 2 1300 | 2.027 | 0.656 |
| Codec 2 1200 | 2.157 | 0.682 |
| A shape 9+9+8, B shape interpolated only | 2.460 | 0.750 |
| A 9+9, B = interpolation + 1.5 x stage 3 | 2.520 | 0.757 |
| the same, B's shape weighted toward the anchor with B's energy | 2.509 | 0.755 |
| A 9+9, B + stage 2 (9 bits, so 49 bits) | 2.549 | 0.764 |
| the same with 6-bit A pitch (48 bits) | 2.520 | 0.765 |
| the same with 2-bit B energy | 2.532 | 0.763 |
| the same with 2-bit B pitch | 2.514 | 0.764 |
| 49-bit stage-2 variant + joint A search | 2.585 | 0.767 |
| **A 9+9, B + 1.5 x stage 3, joint A search (sotto 1200)** | **2.578** | **0.762** |

- **Oracle checks showed where the loss is.**
  - With A as above, giving B its own 34-bit shape gives 2.78. B's shape is the biggest loss.
  - Interpolating between perfect anchors still leaves 3.8 dB rms error per point on B. So B
    needs its own bits.
  - Stage-3 residuals on B bring that to 3.1 dB.
- **What's left on the table.** A codebook trained for this split, especially B's residual,
  would likely gain more. But a new table doesn't fit the K6's flash next to v3.7's.

## C port and the K6 budget

- **Float C** (`c/sotto_lr.c`, `sotto_enc -1200`, `sotto_dec -1200`): on the 16 voices,
  4,493 superframes are 99.6% bit-exact with Python. The misses are v3.7's own pitch-tracker
  near-ties, which `tools/check_c_port.py` shows on v3.7 too. Given the same bits, the
  decoders agree to ≥ 99.7 dB SNR, and ≥ 98.2 dB with 10% superframe loss
  (`docs/results/lr_c_port_check.json`, `docs/results/lr_c_port_check_loss10.json`,
  `tools/check_lr.py`).
- **Fixed point** (`c/fixed/sotto_fx_lr.c`): decoder only. `sotto_lr_enc_create` returns NULL
  in the fixed build, because the K6 only receives. It sits on the v3.7 fixed-point decoder
  core, which now takes per-frame fields (`sotto_dec_frame`). v3.7 and v3.8 outputs are
  unchanged on x86, M4 and M0. The fixed decoder follows the float one as v3.7's does: about
  60 dB SNR, the same PESQ.
- **Cortex-M0 emulator** (`LR=1 sh tools/cm0_build.sh`, `c/fixed/cm0/run.py ELF BITS --lr`),
  -O2, EARS p020:
  - decode is 55.0 MHz mean, 65.5 p99 and 66.9 worst, of 96 MHz. That is 11.5 ms mean and
    13.9 ms worst per 20 ms of output, the same class as v3.7.
  - Its output is bit-exact with x86.
  - Code size: `-DLR_INTERP=0` holds and fades every loss instead of rebuilding, for a
    smaller image.
  - The v3.7 core is now `sotto_dec_frame` plus a bit-unpacking wrapper (`sotto_decode`),
    and the shared part is inlined into both. With `--gc-sections`, an image keeps only the
    entry points it uses. A v3.7-only K6 image grows 88 bytes (-O2). A sotto-1200-only image
    drops `sotto_decode` (680 bytes) and adds `sotto_fx_lr.o` at -Os. With the 20 ms half
    calls the K6 uses, the K6 bench link is 833 bytes more than today's image (701 with
    `-DLR_INTERP=0`), before the pager's own changes. 988 bytes are free.
  - RAM: `struct sotto_lr_dec` is 300 bytes on the M0, besides the v3.7 core's state.

## Use

```sh
make -C c VARIANT=v3.7 all fixed
c/sotto_enc -1200 in.raw out.sot          # 8 kHz 16-bit mono in, 6 bytes per 40 ms out
c/fixed/sotto_dec_fx -1200 out.sot y.raw [good.u8]
python tools/check_lr.py CLIP.flac ... [--loss 0.1] [--dec c/fixed/sotto_dec_fx]
```

API: `sotto_lr_enc_create` / `sotto_lr_encode` (320 samples in) / `sotto_lr_enc_flush`, and
`sotto_lr_dec_create` / `sotto_lr_decode` (one 6-byte superframe in, 320 samples out,
`SOTTO_LR_DEC_DELAY` = 2 superframes behind) / `sotto_lr_dec_flush` (call until it returns 0).
For a player with a 160-sample buffer (the K6), `sotto_lr_decode_half` and
`sotto_lr_dec_flush_half` give the same samples in two 20 ms halves (`sotto_dec -1200half`).
See `c/sotto.h`.

Over the air: `python -m modem.k6 tx msg.sot tx.wav --phy F --payload XL --codec 1200` sends
sotto 1200 frames with magic 0xA9 (`modem/pager.py` `MAGIC_1200`, `profile("XL", "1200")`);
`rx --codec 1200` decodes them.

## Listening round 18

Blind A/B, original in every pair (`tools/listening/render_ab18.py`, key
`/mnt/project-files/vocoder/listening/ab-18-key.json`), Jeff, 2026-11-05:

| Pairs | sotto 1200 wins | ties | other wins |
|---|---:|---:|---:|
| against Codec 2 1200, clean (4) | 3 | 1 (777_m) | 0 |
| against v3.7 at 2400, clean (3) | 0 | 3 | 0 |
| against v3.7, 1 XL packet in 8 lost (2) | 0 | 2 | 0 |
| null pair | | correct | |

Pair 6 (2428_m, with loss) was first marked for v3.7; on a more critical listen Jeff heard
no difference. At half the rate, sotto 1200 ties v3.7, clean and with packet loss, and beats
Codec 2 1200.

## Open

- On the K6, the firmware needs magic 0xA9 and the half-call play path (the K6 thread).
  Flash is tight: 988 bytes free. A sotto-1200-only image needs about 830 of them, and a
  dual image doesn't fit.
