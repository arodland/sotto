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

## The bench test (`c/k6/mod/gp0cap.c`, `tools/k6_ifcap.py`)

On the radio, the two detectors have to meet the same captures at falling signal levels, and
the floor has to be measured. Jeff's rule is no hardware changes, so the module lowers the K6's
own front-end gain instead of using an attenuator.

**`gp0cap`** (API level 2, 2.5 KB) runs DMA channel 1 on PB1 as `gp0dma` does. It counts 64
samples at a time and keeps 2,160 outputs (25 ms, 30 symbols) on the stack, then prints them in
base64 (`c` header and `y` data lines), which takes about 0.45 s at 115200 baud.

- **Pass 0, no carrier:** two captures at each of three gains (the pager's AGC, the highest
  fixed gain, and the AGC table's top entry). This measures the GP0 noise floor.
- **Passes 1 and 2:** the IC-7100's FFSK, then its 4FSK. Each waits for the carrier and skips 2
  s of lead-in, then takes 10 captures at each of 10 gain steps. Step 0 is the pager's AGC. Steps
  1 to 9 are REG_7E fixed (bit 15, index 3) with REG_13 from 0x03FF (everything at maximum)
  down to 0x0018 (LNA short and LNA at their lowest).

The module restores every register it touched and doesn't transmit.

**`tools/k6_ifcap.py`:** `run` sends 65 s of PN9 data in FFSK (peak 0.14), then in 4FSK (peak
0.12), in PKTFM with a CW ID at the end. `analyze LOG` rebuilds each capture and resamples it to
the study's grid at the rate the module measured. It syncs each capture to the known PN9 with
the IF metric (the data-aided timing both detectors then use), trains IF templates on the
strongest step, and prints, per pass and gain step: RSSI, power, a data-aided Es/N0 and C/N0,
and the bit error rate of the discriminator and the IF detector on the same captures. It also
prints the noise floor in the study's units. `simlog OUT.log` writes a log from the model, and
`tests/test_k6_ifcap.py` checks the whole path with it: on a simulated 44 dB-Hz FFSK step the
IF detector makes far fewer errors than the discriminator, on the same captures.

Per step there are about 250 bits (FFSK) or 500 (4FSK), so BER readings below 1% are coarse.
Whether the gain steps are clean dB doesn't matter, because the x-axis is the C/N0 measured from
the captures.

## The receiver on the MCU (`c/k6/ifrx.c`, `c/k6/mod/iflive.c`)

`sim/k6if/mcu.py` is the receiver as the M0 would run it, in float; `c/k6/ifrx.c` is the same in
integers (no division in the per-count path, no statics, so it builds into a module), and
`c/k6/ifrx_tables.h` holds its filter and templates, made by `python3 -m sim.k6if.mcu --tables`.

- **Front end.** A 24-tap complex filter (a 7 kHz low-pass turned up to the 16.9 kHz IF) is
  worked out only at the counts the receiver samples, then turned down by the IF and the AFC
  (a 17-entry quarter-wave sine table).
- **Symbol-locked samples.** 18 a symbol at count `tau + round(i * pc / 18)`, `i = -1 .. 18`
  (the ends are the early and late taps). A fixed decimation by 4 gave 17.87 samples a symbol
  and lost about 1 dB to the FM's phase drift across the symbol.
- **Matched filter.** One template per tone and audio start phase (2 phases for FFSK, 4 for
  these 4FSK tones). The template for a phase half a turn on is the conjugate of the other (to
  0.999), so only half are kept, and four sums per kept template give both correlations. That
  halves the table and the multiplies. Templates are 4-bit re and im (-7..7, a byte a sample):
  about 0.1 dB worse than 8-bit at 1% BER, and 144 B smaller.
- **Timing.** Early-late on the winning template, `(|L|^2 - |E|^2) / 2|C|^2`, with squares (the
  approximate magnitude biased the loop), gain 1/64 (1/16 for the first 128 lead symbols).
- **AFC.** Decision-directed, from the turn between the winning template's two halves.
  Before lock, a frequency-locked loop on the mean turn between samples. FM of zero-mean audio
  keeps the IF's mean frequency on the carrier, but the tones bias the filtered estimate by
  about -100 Hz on the model. So the loop only runs until a sweep has seen the lead.
- **Acquisition.** The symbol clock sweeps 12 offsets, 32 symbols each, scoring the lead's
  fit (tones 0 and M-1 in turn). For 4FSK the fit against the offset is a plateau about a
  third of a symbol wide, and noise puts its best anywhere on it. Its late side is a side
  peak the timing loop holds: locked there, the emulated module ran 0.26 symbol late at 8% BER
  for the whole step. So the receiver walks back from the best while the fit keeps its sign
  and half its size, and takes the plateau's leading edge. On live streams with the measured
  rate one off, taking the best went wrong on 4 to 6 of 18. Taking the edge went wrong on none
  of 54. It takes a sweep when its best clears 48/256
  and its edge agrees, to one step, with the sweep before. A bare carrier scores up to 36,
  random FFSK data now and then more. The threshold was 20 until the model showed a
  carrier-then-lead start locking on a side peak. The clock only moves forward: the old wrap,
  back 11 steps, left the receiver about 300 us of samples to catch up at once, longer than
  the DMA ring lasts. That only showed in the emulated module.
- **Lead check.** 256 symbols steered on the known lead (AFC gain x4). More than 8 misses in
  the first 32, or 16 in any later 32, means a false start: it sweeps again.

On the model, 1% BER (C/N0 dB-Hz, 0.6 s of bare carrier, then a 1.5 s lead, then data; the
`ifrx.c` columns are the median of 4 runs at each C/N0, interpolated):

| | study (float) | `mcu.py` | `ifrx.c` | `ifrx.c`, 164 Hz off |
|---|---|---|---|---|
| FFSK 1200 | 42.5 | 42.7 | 42.9 | 42.7 |
| 4FSK 1200 | 45.2 | 45.1 | 45.6 | 45.5 |

At 44 dB-Hz and 164 Hz off, 2 of the 4 4FSK runs went wrong: one never locked, and one ran at
16% BER.

Lock over 16 runs each at -300, -164, 0, 164 and 300 Hz: FFSK missed 1 (at +300), 4FSK none
(2, at +300, taking the sweep's best; 10 a step past the edge). Without the frequency-locked loop, 4FSK missed 23 of 80, and the -300 Hz runs all
failed. `tests/test_k6_ifrx.py` checks BER, no lock on a bare carrier, and lost counts passed
over without losing the clock.

**`iflive`** (`c/k6/mod/iflive.c` for 4FSK, `iflive_ffsk.c` for FFSK; API level 2, 4072 and
3928 B of the 4072 the store holds) runs the receiver live. It waits for the carrier, times
the DMA (4096 counts on TIM17), then decodes NG = 10 steps of 6 s. Each step is a gain step
as in `gp0cap`, and each starts the receiver over on a fresh lead. So between steps it can
print its line (about 5 ms) and set the gain without keeping the count index across the
pause. The sender's `tools/k6_ifcap.py live 4fsk|ffsk` plays 10 segments of a 1.5 s lead
plus 4.5 s of PN9 in one 60.5 s transmission, then the CW ID. Each step ends 4.5 s after its
lock, before the next lead. The bits go through a self-synchronising PN9 check, so BER is about
check fails / 3 / bits. `livelog LOG` prints the steps as a table.

Fitting it into 4 KB (from 7.6 KB) took these changes:

- the conjugate templates;
- the 4-bit templates;
- one line per step, and no pause bookkeeping;
- the sweep's best kept on the fly, and its step taken from the sample step (no division);
- a quarter-wave sine table;
- `-fomit-frame-pointer` for every module (96 B here).

**Emulated** (`tests/k6_live_emu.py`): the module runs on unicorn's Cortex-M0, at 1.5 cycles an
instruction. That figure is checked against the K6: `gp0dma`, measured on Jeff's radio at
72-73% idle with L = 64, runs at 72% here (and at L = 32 and 128 at 40% and 87%, against 56% and
93% measured, so the emulator errs slow). Its DMA ring is filled from the model's sigma-delta bits at the K6's 5.49 MS/s,
paced by the emulated clock. Over the full 10-step run, 164 Hz off:

- **4FSK at 46 dB-Hz:** every step locked, 8,350 bits a step, BER 0.6-0.9%. Before the
  leading-edge pick, two steps in ten ran near 5%, and CI's 2-step test once locked 0.26 symbol
  late at 8%. At 2 cycles an instruction the same (idle 25%); at 1.2 and on frequency, the
  same, with one false start thrown out by the lead check.
- **FFSK at 44 dB-Hz:** BER 0.2-0.4% on every step.

Both runs lost no counts, used 2.2 KB of stack (the K6 has 3.5 KB at a module's call), and
kept 42% (4FSK) and 51% (FFSK) of the CPU idle.

**Profiled and sped up** (`python3 tests/k6_live_emu.py --module c/k6/mod/iflive.c --profile`:
the PC sampled at jittered intervals; a fixed 256-instruction interval aliases with the
per-count loop). Busy time, 4FSK, before:

| | share of the CPU |
|---|---|
| the 24-tap filter, 20 samples a symbol (`sample`) | 21% |
| counting the DMA's bytes and polling the ring | about 21% |
| the template sums, kept running a sample (`take`) | 9% |
| the rest (decisions, timing, AFC, PN9) | about 8% |

Then:

- **The filter.** The count ring is kept twice over, so the taps walk back without a mask.
  Each tap's re and im share one word (`ifrx_g`), so it takes one load. 20 instructions a tap
  became 13.
- **The template sums.** These are now done twice a symbol, half way and at the end, in
  registers, a pass a component. Before, four running sums were kept in memory for every
  template and sample. That is no faster, but the code is smaller.
- **The key check.** Aura scans the keypad, about 60 us. This now runs a third of the way into
  a symbol, so it never falls on the receiver's bursts. Together with the end-of-symbol sums,
  it had overrun the 186 us ring.
- **Set-up.** One path now covers the receiver's first set-up and each step's, and the host's
  statistics (`IFRX_STATS 0`) are left out of the module.

Now 4FSK leaves 52% idle and FFSK 63%, with no counts lost. Over the full 10-step run (46 and 44 dB-Hz,
164 Hz off) every step still locks, at 0.6-0.9% BER (4FSK) and 0.2-0.4% (FFSK), and the stack
is 2.3 KB. The module is 3994 B (4FSK) and
3846 B (FFSK) of 4072. The counting is the largest remaining cost.

## Jeff's weak-signal idea: PSK on SSB (2026-11-06)

Jeff suggested a weak-signal mode with the IC-7100 in USB data mode sending differential
BPSK/QPSK with RRC pulses, and the K6 working on the same IF tap. The K6 side needs no mode
change. Its IF chain ahead of the demodulator is linear, so the FM set-up with REG_3D at 0x2AAB
is right. The chip's USB mode (REG_3D = 0, zero IF) would fold the sidebands in GP0's real
stream, and REG_47 only feeds the speaker. Turn AFC off (REG_73 bit 4) so it doesn't chase the
signal. Freeze the gain with REG_7E bit 15 (fix index 3) and set it with REG_13's fields. REG_49
holds the RF AGC thresholds.

`sim/k6if/psk.py` models the IC-7100 in USB as a 300-2700 Hz audio filter, then the upper
sideband, scaled to the transmitter's **peak** power. C/N0 here is relative to a carrier at that
PEP, the same footing as the FM study, where the carrier is the whole power. Same GP0 model,
floor 0.05. RRC alpha 0.5 on a 1500 Hz audio carrier, genie timing, and differential detection,
or coherent detection with the carrier phase known (a bound for a perfect carrier loop).

Eb/N0 relative to PEP (dB) at 1% information BER, lower is better:

| Mode (information rate) | uncoded | + r1/2 |
| --- | --- | --- |
| FM: FFSK 1200, IF matched filter (1200 / 600 bit/s) | 11.9 | 10.7 |
| FM: 4FSK 1200 baud, IF matched filter (2400 / 1200) | 11.6 | 10.3 |
| SSB: DBPSK 1200 baud, differential (1200 / 600) | 11.4 | 10.5 |
| SSB: DQPSK 1200 baud, differential (2400 / 1200) | - | 10.7 |
| SSB: DQPSK 600 baud, differential (1200 / 600) | 11.8 | 10.1 |
| SSB: BPSK 1200 baud, coherent, ideal carrier (1200 / 600) | 9.6 | **7.0** |
| SSB: 16FSK 100 baud, tones 100 Hz apart, noncoherent (400 / 200) | **8.1** | 8.4 |
| SSB: 8FSK 150 baud (450 / 225) | 8.9 | 8.7 |
| SSB: 4FSK 300 baud (600 / 300) | 10.1 | 9.6 |

- **At equal transmitter peak power, differential PSK only ties the FM modes with the IF
  detector.** RRC BPSK through the SSB chain averages 3.4 dB below its peak. That cancels
  coherent PSK's usual 5-7 dB edge over discriminator FSK, because the IF matched filter has
  already taken that edge.
- **Coherent BPSK with a perfect carrier loop is the best of them coded:** about 3.7 dB better
  than coded FFSK on FM. A real loop at 440 MHz on a 2.5 ppm crystal (about 1 kHz to pull in,
  plus phase noise) would give some of that back.
- **Constant-envelope MFSK on SSB** (one tone at a time is just a carrier, so the full PEP, as
  FT8 and JS8 do) gets close without any carrier sync. 16 tones at 100 baud is 3.8 dB better
  per bit than uncoded FFSK on FM, and it tolerates drift.
- For a weak-signal mode the larger gains come from the rate. Each halving of the rate buys 3
  dB of C/N0, on top of the per-bit numbers here.

So the weak-signal mode we'd recommend is SSB MFSK (16 tones, about 100 baud) with soft FEC,
using the same IF tap and the same host decoder on the bench. Coherent BPSK is the stretch goal
once the K6's frequency error and phase noise are measured from captures.
