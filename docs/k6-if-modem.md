# K6 softmodem from the IF: a study

Since PR #68/#69, the K6's MCU can read the FD6818's receive IF. GP0 code 13 is a 1-bit
sigma-delta stream of the ~16.9 kHz low IF on PB1, and DMA channel 1 samples it flat out at
about 5.5 MS/s. So the K6 no longer has to take received data from the chip's FFSK decoder,
which slips its bit clock at 2400 baud and sits behind an FM discriminator. This page asks how
much better a receiver working on the IF itself can do. Jeff's thought (2026-11-06) was that
sampling I/Q might gain sensitivity. A real IF sampled this fast already holds what I/Q would,
so the gain has to come from skipping the discriminator, and that is what the simulation
measures.

Everything here is **simulation** (`sim/k6if/`) until the bench run below is done.

## The model

`sim/k6if/frontend.py` and `sdm.c` model the GP0 path, and `study.py` and `modes.py` model the
link and the receivers.

- **Sender:** audio-tone CPFSK at 48 kHz (profile F's phase-continuous tones, or four tones).
- **IC-7100 in FM-D from USB audio (run 9's sweep):** +6 dB/octave emphasis between 600 Hz and
  2.6 kHz, a 300 Hz high-pass, 18 kHz of deviation per unit at 1 kHz, the ±4.3 kHz limiter, and
  a 3.1 kHz wall after it. The fit is within 2-3 dB of run 9's table, and a bit gentler below
  300 Hz.
- **K6:** AWGN at a given C/N0, then the chip's channel filter (±8 kHz, a guess). The AGC holds
  the receiver's own noise at a fixed rms on the IF ("floor") and backs off only for strong
  signals, with a strong carrier at density 25-75%. The real IF is at 26 MHz / 1536 = 16.927
  kHz.
- **GP0 path:** a second-order 1-bit sigma-delta at 6.5 MHz (run 6's 156 ns run lengths),
  sampled at 5.53 MS/s with ±15% timing jitter around a fixed grid, and counted 64 at a time as
  `gp0dma` does (86.4 kHz).

**Calibration against run 6 (bare carrier, FM noise in a ±6 kHz band, Hz rms):**

| Sample rate | 22.9 MS/s | 11.4 | 5.7 | 4.6 | 2.9 |
| --- | --- | --- | --- | --- | --- |
| measured | 60 | 89 | 166 | 413 | 625 |
| model | 46 | 91 | 191 | 301 | 649 |

The 1-bit path alone gives a carrier-to-noise floor of about 62 dB-Hz at the working set-up.
An earlier model with random-walk sampling jitter (an unanchored DMA clock) came out 20 dB worse
and is wrong: the DMA runs off the crystal.

**The unknown that matters most is the floor.** gp0tone run 4 saw the code 13 density "flat at
50%, a few % of noise" with no carrier. In the model, a real-IF noise rms of 0.03 to 0.06 gives
a density spread of 2 to 3%. The 1-bit path's own noise is about as loud as the receiver's at
those levels, so it costs sensitivity. The tables give floor 0.05 (about 1.5-2 dB lost against
no sigma-delta at all), 0.03 (about 4 dB lost), and the ideal case.

## The receivers

The counts are mixed down from 16.927 kHz and decimated 4:1 to complex baseband at 21.6 kHz (18
samples a symbol at 1200 baud). Both receivers get the same timing (found once on a clean
capture) and see the same samples.

- **disc:** the best FM receiver we could build in software, not the chip's own. A 6th-order
  low-pass at Carson's bandwidth, a limiter-discriminator (the angle between successive
  samples), then noncoherent correlators for each audio tone over each symbol, picking the
  largest. The chip's FFSK decoder is also discriminator-based and adds its bit-clock slips, so
  in practice it can only do worse.
- **IF:** a matched filter on the complex IF. For each tone, and each audio phase at the symbol's
  start (two for FFSK, four for the 4FSK set), a template of the received IF waveform is trained
  on a clean capture. Each symbol's metric is the largest |correlation| over its phase classes.
  This uses all of the FM signal's energy, and so has no FM threshold.
- **Coherent IF:** decision-aided carrier phase tracking. This was no better than noncoherent
  on FFSK, so it is parked.
- **+r1/2, +r1/3:** max-log soft values per bit (Gray-mapped for 4FSK) into the K=7 tail-biting
  convolutional code from `modem/fec.py` (the FEC race's winner), in blocks of 240 info bits.

## Results

C/N0 (dB-Hz) at 1% **information** bit errors, 6,000 symbols per point (`python3 -m
sim.k6if.modes ffsk1200 4fsk1200 [--floor 0.03 | --ideal]`; raw BER curves: `python3 -m
sim.k6if.study ber`). Lower is better: 6 dB is about twice the
free-space range.

| Mode, information rate | disc | IF | disc + r1/2 | IF + r1/2 | IF + r1/3 |
| --- | --- | --- | --- | --- | --- |
| FFSK 1200 (today's F/1200), 1200 bit/s | 48.8 | **42.7** | 46.3 (600 bit/s) | **38.5** (600) | 36.5 (400) |
| 4FSK 1200 baud (900/1500/2100/2700 Hz), 2400 bit/s | 50.6 | **45.4** | 47.9 (1200) | **41.1** (1200) | 40.2 (800) |

Floor 0.05. With floor 0.03 every number is about 2 dB higher. With no sigma-delta every number
is 1.5-2 dB lower, and the gaps between columns stay the same.

- **The IF matched filter beats the best discriminator by 5-6 dB** on the same signal. That is
  the FM threshold: the discriminator falls apart below about 10 dB CNR in Carson's band, and
  the matched filter keeps going.
- **The code adds 4 dB on top**, but only after the IF detector. Behind the discriminator, its
  click noise leaves soft decoding with 2.5 dB.
- **For sotto 1200 live (1200 bit/s): 4FSK 1200 baud + r1/2 + IF detector needs 41.1 dB-Hz**,
  against 48.8 for FFSK 1200 through a discriminator with no code, which is roughly today's
  pager. That is about **7.7 dB**, or 2.4 times the range, at the same audio quality.
- **MSK at 2400 baud (1200/2400 Hz) is bad on this transmitter:** 4% bit errors even at 52
  dB-Hz. Its 2400 Hz tone and the edges of its short symbols run into the IC-7100's 3.1 kHz
  wall and limiter. This matches the 2400 baud trouble on air. Four tones at 1200 baud carry the
  same 2400 bit/s inside the passband.
- **Deviation helps the IF detector, up to the limiter.** The 4FSK level sweep (1200 baud, BER
  at 45 dB-Hz) gave peak 0.07 (±2.7 kHz) 6.7%, 0.09 3.1%, 0.12 (±4.5 kHz) 1.1%, 0.16 0.9%. The
  last one leans on the limiter. Tones 900-2700 Hz beat 800-2600 and 1000-2500.
- **sotto 2400 uncoded** would ride 4FSK 1200 at 45.4 dB-Hz, 3.4 dB better than F/1200 through a
  discriminator, at twice the rate.

Not modelled yet: timing recovery and template estimation on the radio (probably 0.5-1 dB),
frequency offset (the IF measured 16.92-16.96 kHz, so tens of Hz, harmless over a symbol),
fading, and the real chip filter and AGC.

## CPU on the K6

The study's receiver at 1200 baud, per second: 4FSK templates 16 x 18 complex MACs x 1200 = 1.4
M MACs, about 6% of the M0 at 96 MHz, plus 2-3x that for early/late timing. The K=7 Viterbi at
1200 bit/s is about 1%, and mixing and decimation 2%.

The DMA count loop is the expensive part: `gp0dma` spends 27% of the CPU on it. An unrolled
loop doing a word load and a masked add per 4 samples would be about 1 cycle a sample, or 6%.

Playing sotto 1200 at the same time costs 53-61 MHz mean on the M0. So a live voice pager on
this path would sit at about 80-90%: tight, but possible once the count loop is fast.

## The bench test

On the radio, the two detectors have to be compared on the same captures at falling signal
levels, and the floor has to be measured. Jeff's rule is no hardware changes. So instead of an
attenuator, the module will lower the K6's own front-end gain: REG_7E fixed gain, with REG_13's
LNA, mixer and PGA steps per egzumer's table. It records the GP0 counts at each step and sends
them to the host, which runs exactly these receivers on them and measures C/N0 from the IF
itself (the carrier against the empty band beside it). Both detectors see the same captures, so
the comparison holds even if the gain steps aren't clean dB.
