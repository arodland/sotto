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
  approximate magnitude biased the loop), gain 1/64 on the lead and 1/256 once tracking
  (`IFRX_T_SH`). It was 1/16 for the first 128 lead symbols until 2026-11-07: for FFSK that
  is a per-symbol gain past 2, an unstable loop, which with the K6's detector noise locked FFSK half a
  symbol off (see the rerun below).
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
- **Lead check.** 256 symbols steered on the known lead (AFC gain x4 for the first 128, x1
  for the rest, then x1/4 once tracking, `IFRX_F_SH`). More than 8 misses in the first 32, or
  16 in any later 32, means a false start: it sweeps again.

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

## First runs on the bench (2026-11-06, 21:03-21:22 UTC)

The K6 only received; the IC-7100 sent everything on 440.850 (FM-D, 12 kHz passband), with
the pager image 888ccb0 and its CI modules.

**`gp0cap` (ret 2):** all 206 captures whole. The IF detector beats the discriminator at every
level. FFSK has no errors in either detector down to -72 dBm (C/N0 50.7 dB-Hz). 4FSK has an
error floor of 6-8% (IF) and 8-10% (discriminator) at every level, even -23 dBm, so it isn't
noise. (A later analysis said this floor was the analyser's own detector; that analysis read
low. Held out properly, the MCU receiver's templates give 5.7% on these captures: see "The
rerun".) Nearly all the IF detector's
errors are an inner tone read as the outer tone next to it: 1500 as 900 (SER 0.25) and 2100 as 2700 (0.26). The outer tones are nearly clean (0.025,
0.009), and the error doesn't depend on the tone before. That fits the IC-7100's TX chain
(about +5 dB/octave of pre-emphasis, limiting near ±4.3 kHz in run 9) squeezing the deviation
spacing, which 0.12 peak puts at or past the limiter at 2700 Hz. Next: the 4FSK pass at a
lower level, and pre-distorted tone levels (sender side only). The noise floor with no
carrier is 0.062 rms in the study's units (it assumed 0.03-0.06).

**`iflive` 4FSK (hung):** it timed the DMA at **5875 kS/s** (gp0dma saw 5490, gp0cap 5555
the same evening), printed its first line, and then went silent until the K6 was power cycled.
Two faults:

- **The DMA's rate moves with what the CPU does.** gp0dma's probe (run 8) already saw 5.47 to
  5.80 M transfers/s depending on what the CPU was doing, and iflive measured it while only
  polling the ring. The receiver can't take that error: on the model it acquires only within
  about ±0.05% of the true rate (a sweep's 384 symbols drift past the ±1-offset agreement),
  though once locked the timing loop holds 1% easily. So the module now measures the rate as
  it decodes, every 131 ms of TIM17 (at 1 MHz), and hands it to the receiver
  (`ifrx_rate`). Each step starts on the last step's rate, and the first on `KS0` (5530).
  The step line now ends `k=` (kS/s).
- **A look at the ring more than a lap late broke the count index.** The ring's laps were
  counted from the DMA pointer wrapping, so a gap longer than the ring (186 us) lost a lap.
  Then `produced < consumed`, the next overrun skip asked the receiver to pass over about 2^26
  counts (about a second of `advance` calls), and that stall lost laps again: the likely
  hang. The module now checks the time between looks: past `STALL_US` (120 us) it works out
  the laps from TIM17 and the rate, so nothing worse than an overrun skip can follow. The key
  and deadline checks also run when no counts come for a while, and after at most 1024 counts
  even if the receiver never reaches its check point.

On the emulator (`tests/k6_live_emu.py --fdma 5.875e6 --stall 40,400`), 10 steps at 48 dB-Hz
164 Hz off lock every step at 5490 and at 5875 kS/s, at about 0.15% BER, the latter with a
400 us stall every 40 key checks. FFSK at 45 dB-Hz: 10 of 10 at 0.1-0.5%. The module is
4042 B (4FSK) and 3894 B (FFSK). The rate measurement is kept off the key check (its
divisions take about 25 us), and the head line `live ks=... rx=...` is gone: the module ran
out of room. The emulator's idle reads 46% (4FSK) and 56% (FFSK), against 52% and 63% before:
partly TIM17's coarser 1 us tick in the idle count, not yet pinned down.

**`afsweep` (ret -6):** the FM pass finished (AF 0-15 look alike in RSSI, noise, glitch and
AF level; only AF 15 peaks higher), but with the AM register set-up (REG_14 0, the AM REG_49,
AFC off) the K6's RSSI never reached floor+15 on the IC-7100's AM-D carrier, which read -23 dBm
in FM. So the USB pass never ran. Something in that set-up kills the RSSI reading (or the
front end); it needs a module change before a rerun.

The sender tools now put the IC-7100 in FM-D with the 12 kHz filter: `rigctl M PKTFM 0`
selects the 6 kHz one, at about half the deviation.

## The rerun, and what the live errors were (2026-11-07)

Jeff power-cycled the K6 at 01:12 UTC. The K6 only received; the IC-7100 sent `live 4fsk` and
`live ffsk` on 440.850 (FM-D, 12 kHz passband), into iflive from PR #81 (image 1a89fef). The
step lines are in `tests/data/k6/iflive-20261107.log.gz`, the gp0cap captures of 2026-11-06 in
`tests/data/k6/gp0cap-20261106.log.gz`.

- **No hang.** Both runs finished all 10 steps. `k=` was 5566 on every step that locked.
- **4FSK:** steps 0-8 locked, at 4.4-5.0% BER (check fails / 3 / bits), with the AFC ending
  anywhere from -39 to +29 Hz. Step 9 (RSSI -98 dBm) never locked.
- **FFSK:** steps 5-7 had no errors, with the AFC at +5 Hz each time. Steps 1-2 ran at 1.5-1.9%,
  steps 0 and 3 at about 5%, and steps 4 and 8 never locked (nor 9).

**The captures were fine** (wrong: see the correction below; that analysis reads low).
`k6_ifcap.py mcu LOG` puts the captures through the MCU receiver's
own templates, made from the model, at the best timing and IF for each capture. FFSK had no
errors down to -72 dBm, and 4FSK had 0.2-0.8% at every level. Templates trained on the captures
did no better. So the 6-8% 4FSK floor in the first run's report came from the analyser's
detector, and the limiter idea was wrong.

**The detectors are much noisier on the K6 than on the model.** At the same best timing and IF,
the per-symbol outputs of the timing detector, `(|L|^2 - |E|^2) / |C|^2`, and the AFC detector,
`Im(c2 c1*) / mean |c|^2`, were:

| | timing sd | AFC sd |
|---|---|---|
| K6, FFSK, steps 1-8 | 0.76 | 0.36 |
| K6, 4FSK, steps 1-8 | 0.45 | 0.45 |
| model at 70 dB-Hz, FFSK | 0.06 | 0.09 |
| model at 70 dB-Hz, 4FSK | 0.11 | 0.12 |

The noise is white from one symbol to the next, and the symbol pattern (the symbol before, this
one, the one after) explains only 0-16% of it, so it isn't template mismatch. The model only
gets that noisy near 42 dB-Hz, where its FFSK decisions already make 1% errors, so it isn't plain
receiver noise either (`docs/k6-modules.md`, insight 27).

**Through the old loops, that noise is enough.** With noise of those sizes added to the host
receiver's detector outputs (`ifrx_host.c`, info[9]), on the model at 70 dB-Hz:

- **The lead's timing loop was unstable.** At 1/16 its gain a symbol was about 3 for FFSK
  (1.3-2 for 4FSK), past the 2 where a loop stops settling. Clean signals got away with it. With the K6's
  noise, FFSK locked 0.4 symbol off at 50% errors in 2 of 4 runs. That is the FFSK steps on the
  bench that went wrong or never locked. The lead now runs at 1/64 throughout.
- **The tracking loops followed the noise.** 4FSK's AFC wandered 21 Hz rms (on the bench the
  steps ended -39 to +29 Hz), and its timing 0.50 half-samples rms (FFSK 16 Hz and 0.72). At
  1/256 timing and a quarter of the AFC gain, that becomes 8 Hz and 0.25 half-samples (FFSK 6 Hz
  and 0.36). The timing still follows a 100 ppm clock error between the two radios to 0.04
  half-samples.

Applied to the captures (that jitter around each capture's best), the old loops' jitter costs
4FSK about 1.5% BER and the new one about 0.7%. The rest of 4.5% may be where the detectors settle
on real signals: their zeros sit 0.3 half-samples early and 15 Hz high of the captures' best,
worth about 1.3% at full size. That number leans on a best picked from only 26 symbols a
capture, so it overstates. `tests/test_k6_ifrx.py::test_loops_through_the_k6s_detector_noise`
keeps the new loops honest. The modules are 4062 B (4FSK) and 3914 B (FFSK).

**On the bench (2026-11-07, 02:55-03:00 UTC, PR #88's CI modules, receive only):**

- **FFSK:** no errors on steps 0-5 and 7, and 3 in 5,147 bits on step 8. Step 6 threw its lock
  out (`j=1`) and didn't find another before the step ended. Before, steps 0-3 ran at 1.5-5%
  and steps 4 and 8 never locked.
- **4FSK:** 3.6-5.0% on steps 0-8, against 4.4-5.0%. The AFC now ends every step within
  -12 to +4 Hz but one (+29), where it ran -39 to +29.

So the loops' jitter was FFSK's whole problem, but not 4FSK's. What's left for 4FSK is about
the same on every step and gain, which points at where the loops settle rather than at noise.
On the captures the timing detector's zero sits about 0.3 half-samples from the best decisions.

**`iflive_tsweep`** (`c/k6/mod/iflive_tsweep.c`) settles that on the bench. It is the 4FSK
receiver with `(step - 4) * 600` (Q12) added to the timing detector's output, so the loop
settles a different amount early or late on each step: from about one half-sample early (step
0) to one late (step 8), with step 4 the plain receiver. Every step runs at gain step 2. Its
line has no `r=` or `i=` (no room). The bias applies only once tracking: on the lead, ±1200 and more threw the lock out. On the
emulator (47 dB-Hz, 164 Hz off), steps 0-8 gave 10.1, 9.7, 5.4, 0.68, **0.30**, 0.45, 3.4, 9.6
and 9.7%: the model's best is the plain receiver, as it should be. On the K6 (2026-11-07, 03:14 UTC, receive
only) steps 0-9 read 10.5, 10.7, 9.6, 8.7, 4.6, **3.4**, 8.7, 10.0, 10.0 and 10.1%: the best is
one notch late, and no setting gets near the captures' 0.4%. So timing isn't 4FSK's floor.

**The K6's own C decisions on the captures are fine.** `ifrx.c` built for the host with its
loops frozen (`ifrx_static`, scratch harness), at the best timing and IF it finds for each
capture, gives 4FSK 0.32% on the captures, against 0.41% for the float analysis. FFSK has no
errors either way. So the integer code doesn't make the floor (both figures carry the same selection bias, see
below, so they compare fairly with each other but not with the bench). What's left is something only
the live receiver sees: the AFC's settling point (the captures put the AFC detector's zero
15 Hz off the best), or the stream itself while the receiver runs.

**`iflive_fsweep`** (`c/k6/mod/iflive_fsweep.c`) tries the AFC. Steps 0-4 add
`((step % 5) - 2) * 240` (Q12, about 27 Hz a notch) to the AFC detector, and steps 5-9 the
same with tsweep's best timing (+600). Every step runs at gain step 2.

On the K6 (03:25 UTC, receive only) the AFC offset made no difference (3.7-4.1% across ±2
notches at the plain timing), and the +600 timing took every step down a point (2.5-3.5%, best
2.47%). The DMA ran at 5454 kS/s, against 5618 for tsweep and 5567 for PR #88's modules: the
module's code layout alone moves the free-running rate by 3%. On the emulator, though, a DMA 7%
faster while the CPU polls than while it computes (`tests/k6_live_emu.py --wobble 0.07`, the
sigma-delta's bits then taken at their own 6.5 MHz clock where the emulated bus puts each read)
leaves both modes error-free. So the rate's swing within a symbol isn't the floor.

**Correction: the captures don't decode at 0.4%.** `mcu_analyze` picks each capture's timing,
IF and PN phase as the best of about 900 tries on the same 26 symbols it then counts. That
reads far too low. `k6_ifcap.py mcu --xval` picks them on one half of each capture (by the
known symbols' share of the metric) and counts the other half. On the 2026-11-06 captures,
steps 1-8:

| | held-out BER | errors, sent -> got |
|---|---|---|
| FFSK | 0.43% | 1800 Hz as 1200 (9) |
| 4FSK, model templates | 5.7% | 900 as 1500 (137), 2100 as 2700 (44), 1500 as 2100 (23), 900 as 2700 (16) |
| 4FSK, templates trained on step 1 (tested on 3-8) | 3.6% | 900 as 1500 (58), 2100 as 2700 (16) |

So the live receiver's 2.5-4.6% is about what these templates get from this signal, and the
loops were never the main loss. The floor is in the signal and the templates' fit to it. Per
tone, the IC-7100's deviation on the captures (the IF's instantaneous frequency fitted over
each known symbol) is about 77% of the model's for 900-2100 Hz and 69% at 2700 Hz:

| tone | 900 | 1500 | 2100 | 2700 |
|---|---|---|---|---|
| K6 captures, Hz | 1447 | 2096 | 2541 | 2036 |
| model, Hz | 1886 | 2702 | 3219 | 2937 |

The receiver's templates expect the model's deviation. Two fixes are open: send louder (the
sender only, `k6_ifcap.py live 4fsk --levels ...`), or train the templates on the K6's own
captures. **`iflive_g2`** (`c/k6/mod/iflive_g2.c`) is the plain 4FSK receiver at gain step 2
on every step, for a level sweep from the sender.

### The level sweeps (2026-11-07 03:43 and 03:50, PR #92)

Both runs used `iflive_g2` with IC-7100 USB MOD at 30%, RSSI -61 dBm, the K6 receiving only.
The levels are the WAV's peak tone level; the mode's own is 0.12.

| level | 0.02-0.045 | 0.065 | 0.10 | 0.12 | 0.14 | 0.16 | 0.18 |
|---|---|---|---|---|---|---|---|
| BER | no lock (lead fit 4-53) | 4.15% | 3.2-4.7% | 3.7-4.0% | 3.8-3.9% | 4.2-4.3% | 3.9-4.4% |

The floor doesn't move with level. Off the radio, on the 2026-11-06 captures (`gp0cap` at
0.12), fitting the IF's instantaneous frequency to the sent tones through a short FIR:

- The residual beyond the FIR's fit is about 600 Hz rms (model 400-500 Hz, the tap's
  sigma-delta noise). The excess sits at the tones, 1.5-3 kHz: 440 Hz rms there against 245 in
  the model. Below 600 Hz (phase noise, drift) the two match.
- Adding the 2nd and 3rd harmonics explains only 0.4-1.4 dB of the residual (model 0.3-0.9), so
  it isn't plain clipping.
- Each symbol's deviation, against the fit, varies 14% rms (model 6%), white from symbol to
  symbol.
- The model with the IC-7100's lower deviation (x0.73) and no other change gives 0.05% at
  70 dB-Hz. With narrower chip filters (6th-order, 3-5 kHz) it gives 0-1.4%. Neither makes 4%.

So something adds tone-band distortion that grows with the signal. Lead fit (q) rises with
level up to 0.10 and is flat above, which hints at a limiter near 0.10 at 30% USB MOD. But the
BER is the same at 0.065, below that. **`k6_ifcap.py run --level X`** scales the 4FSK capture
pass, so `gp0cap` at a few levels can measure the deviation and this distortion against level
directly.

### Captures at three levels (2026-11-07 04:29-04:40)

`gp0cap` with `run --level` 0.07, 0.12 and 0.18 (logs in
`tests/data/k6/gp0lvl-*-20261107.log.gz`), steps 1-8:

| level | 900 Hz | 1500 Hz | 2100 Hz | 2700 Hz | residual 1.5-3 kHz | per-symbol deviation sd | held-out BER |
|---|---|---|---|---|---|---|---|
| 0.07 | 1243 | 1917 | 2343 | 1809 | 411 Hz | 13.8% | 6.2% |
| 0.12 | 1464 | 2101 | 2531 | 2042 | 450 Hz | 14.4% | 5.4% |
| 0.18 | 1490 | 2085 | 2565 | 2000 | 438 Hz | 14.3% | 5.4% |

(Per-tone deviation in Hz.) **The IC-7100 limits deviation.** From 0.12 to 0.18 (x1.5) it rises
0-2%; from 0.07 to 0.12 (x1.7) only 10-18%. The limiter makes few harmonics, so it acts like a
gain control rather than a clipper. At 30% USB MOD the sender already sits at its ceiling.

**The extra noise doesn't follow the signal.** It is the same at all three levels. Its mean over
a symbol, given the symbol before and after, explains none of it on held-out symbols (624 Hz rms
-> 625-640), so it isn't intersymbol interference. Model templates rebuilt at the measured
deviation (all tones x0.77, or per tone) give 5.2-5.9% held out against 5.4%, so it isn't the
templates either. The confusions lean one way (900 Hz read as 1500 about 120 times, the
reverse about 8; 2100 as 2700 about 40, the reverse 3-10). 900 Hz has the smallest deviation
and 2700 Hz the next smallest.

What's left is FM noise of about 450 Hz rms in 1.5-3 kHz, constant whatever the modulation: the
signature of synthesizer noise (a PLL's phase noise peaks near its loop bandwidth) in the
IC-7100's transmitter or the K6's receiver. **`k6_ifcap.py fmnoise LOG`** measures it on a bare
carrier (`run --level 1e-6` sends one in place of the 4FSK pass). The model's tap alone gives
2, 6, 27, 96, 251, 242 Hz rms in 0-50, 50-200, 200-600, 600-1500, 1500-3000 and 3000-4500 Hz. If
the bare carrier shows the bump, a K6-to-IC-7100 capture would tell which radio adds it.

### A bare carrier, and the AFC off (2026-11-07 05:09-05:20)

- **Bare carrier** (`run --level 1e-6`, `tests/data/k6/gp0carrier-20261107.log.gz`): `fmnoise`
  gives 0, 1, 3, 6, 12, 14 Hz rms at steps 1-5 (-61 dBm), rising to 36 and 96 Hz in 600-1500 and
  1500-3000 Hz at step 8 (-72 dBm). So **neither radio's synthesizer is the noise**: it shows only
  under modulation. (The tap's sigma-delta noise on a steady IF tone is lower than the model's,
  which whitens it; under modulation the model gives 250-300 Hz.)
- **AFC off** (`gp0cap_afc`, r73=4692, `tests/data/k6/gp0afcoff-20261107.log.gz`): the
  residual is the same (528 Hz rms against 539 with the AFC on), and so is the per-symbol
  wobble (13.7%) and the 4FSK held-out BER (5.1%). FFSK read 0.29% (6 errors in 2080 bits)
  against 1.0% with the AFC on. **The K6's AFC isn't the 4FSK floor.**
- With a 1.4 ms FIR shared by all captures (the sender's audio response, fitted), the residual
  is 530-540 Hz rms against 276 in the model. Given that FIR, the previous symbol and the audio
  phase explain none of what's left (530 -> 519-533 held out). Without it they explain a
  good part (627 -> 504), because the IC-7100's audio response isn't the model's. The IF's
  amplitude falls to 0.7 at +-4 kHz, as in the model.

So about 450 Hz rms of FM noise at the tones comes with the modulation. It scales with the
signal (the 14% wobble holds at 0.07, under the limiter, and at 0.18) and is white from symbol
to symbol. It isn't intersymbol interference, the AFC, a synthesizer or the K6's channel
filter. It most likely comes from the IC-7100's transmit audio path (USB codec, its DSP, the
modulator). Telling the two radios apart needs another transmitter, or another receiver of the
IC-7100's signal, or the IC-7100's ACC input instead of USB audio.

### Templates trained on the K6 (`iflive_k6t`)

Whatever adds the noise, the K6 can fit the signal it gets better than the model does. On 320
captures (2026-11-06 and the 0.12, 0.18 and AFC-off runs, steps 1-8), trained on half of them
and tested on the other half, with timing and IF picked on one half of each test capture:

| 4FSK templates | held-out BER |
|---|---|
| the model's (c/k6/ifrx_tables.h) | 5.5% (4-bit: 5.6%) |
| trained per tone and start phase | 2.8% |
| the same, each phase folded with its conjugate (as the C keeps them) | 2.4% (4-bit: 2.3%) |
| trained per previous tone and tone, with decision feedback | 6.3% (too few symbols a class) |

**`k6_ifcap.py tables LOG...`** writes c/k6/ifrx_tables.h with a second 4FSK set trained on
the given captures (`mcu_train4`), used by builds with `IFRX_T4_K6`; the model's tables are
unchanged otherwise (`python3 -m sim.k6if.mcu --tables` still writes them alone). The committed set is
trained on all four logs above (8320 symbols). **`iflive_k6t`** is `iflive` with that set.
On the emulated model at 52 dB-Hz it locks every step at 0.4-1.3% (the model's signal isn't
what it was trained on); the bench is what counts.

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
