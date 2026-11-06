# FT8 and WSPR on the K6

Jeff, 2026-11-06: "It would be interesting to develop ft8, WSPR plugins for this firmware."
This page covers what was built, what the simulation and the emulator say, what fits on the
radio today, and what is still waiting on a bench run, a firmware change or a decision.

## Summary

- **FT8 receive works end to end in simulation and on an emulated M0.** The receiver chain is:
  the IC-7100 sends USB, the K6's IF goes through the 1-bit GP0 path and the DMA ring, then the
  M0 code mixes, filters and builds the waterfall, and the same C core decodes it.
  `tests/test_k6_ft8.py` runs every stage, including the `ft8wf` module on unicorn down to
  "CQ AG7EW CN87".
- **Sensitivity (simulated, 40 trials each, random start time and frequency):**
  - Clean audio into the decoder: 50% at about -17.5 dB SNR (2500 Hz).
  - Through the K6's GP0 path with the gain set-up: 75% at -16 dB and 95% at -14 dB.
  - WSJT-X reaches about -21 dB. The difference is the cost of a fixed-point decoder with 4-bit
    waterfall values and no a-priori passes.
- **What fits on the radio today:**
  - `ft8wf` (3.8 KB of code) fits the 4 KB module store. It runs the front end and the waterfall
    on the K6 and streams the waterfall over the serial report. `tools/k6_ft8.py decode LOG`
    decodes it on the PC with the same receiver.
  - `ft8rx`, the whole decoder on the K6, builds at 7.2 KB of code and needs about 4 KB of RAM
    besides. That doesn't fit until the firmware gives modules a bigger arena (see "Getting
    ft8rx onto the radio").
- **Bench run 1 (2026-11-06): no decodes.** The K6's IF offset is only +11 Hz, but the DMA
  ring overran about 6,450 times a second because the stop-key scan (about 60 us on the radio)
  ran on every pass of the drain loop. Fixed for run 2; see "Bench run 1".
- **FT8 transmit:**
  - Planned as AFSK on FM through the chip's tone oscillator (REG_70/71). A 12.64 s FT8
    transmission plus a CW ID fits the 20 s limit of the level 3 TX API (PR #71).
  - Untested: PR #71 isn't flashed yet, and `tx_on`'s REG_30 may not route the tone to the
    carrier (see "FT8 transmit").
- **WSPR:**
  - Receive is feasible in RAM and CPU.
  - At 440 MHz it hinges on the K6's crystal drift over 2 minutes, which nobody has measured yet.
  - Transmit needs about 111 s of keying, more than the 20 s TX limit. That is Jeff's call.

## The receiver

```
PB1 (GP0 code 13: the FD6818's low IF at 26 MHz / 1536 = 16.927 kHz as a 1-bit sigma-delta)
  -> DMA channel 1 flat out (~5.5-5.7 MS/s) into a 512-byte ring          gp0rx.h
  -> counts of 64 samples (~86 kHz), each with the mixer phase for the moment it was taken
     (TIM17 at 96 MHz, unwrapped with Aura's 100 us TIM14; KMIX = (IF + 1500 Hz) / 96 MHz)
  -> complex mixer -> CIC 16 -> CIC r2 (~27) -> ~200 Hz complex           ft8k.c fk_in
  -> every 40 ms: 32-point DFT (6.25 Hz bins), max with the half-bin, max of two
     half-rows -> log2 power -> 4-bit companded row of nb bins            fk_rowq
  -> 162-row ring; Costas sync score for a start 156 rows back, at every row (no UTC
     clock, so transmissions can start whenever they like)                fk_putrow
  -> local maxima over threshold -> 174 max-log LLRs -> layered normalised min-sum LDPC
     (x 7/8, 30 iterations, ft8_lib's tables) -> CRC-14 -> 77 bits -> text
```

- **Fixed point for the M0.** The code has no divide instruction, no libraries, no writable
  statics and no 64-bit arithmetic. Every buffer is passed in by the caller, and clang must not
  turn a loop into `%`, which `slot_add` and the 15 s report counter are written to avoid.
- **The window.** It is nb bins of 6.25 Hz around FT8_AUDIO (default 1500 Hz, a USB dial
  offset). With nb = 32 the window is 200 Hz wide; one FT8 signal occupies 50 Hz.
- **The gain set-up (gp0rx.h `rx_gain`).** The simulation says the 1-bit path costs 2-4 dB when
  the receiver noise sits at the pager AGC's level (floor 0.05), and about nothing with the
  noise near 9-10 counts rms (floor 0.15). So the module:
  - measures the counts' rms;
  - if it is under 8.0, fixes the AGC (REG_7E bit 15, index 3) and takes the first REG_13 gain
    (03FF, 03BE, 037E, 033E) whose noise is under 14.0 rms;
  - puts everything back when it exits.
- **Pauses are zero-filled.** While the CPU is away (a report line, a decode) the DMA laps the
  ring unseen. `drain` feeds zeros for the counts it missed, so the 200 Hz samples stay evenly
  spaced in real time. The first version skipped them instead, which slipped the clock and
  smeared the tones. The simulation, with one 16 ms line every 160 ms:

  | Pauses | -14 dB | -16 dB |
  | --- | --- | --- |
  | none | 95% | 75% |
  | skipped (the first version) | 42% | 20% |
  | zero-filled (now) | 90% | 68% |

  A 30 ms pause, zero-filled, gives 88% / 45%. `ft8k_host counts ... gap=MS` models this.
- **The sideband.** If the IF turns out mirrored (the tone at IF - f), build with FT8_INV 1. The
  mixer then runs at IF - 1500 Hz with the phase negated; both are tested.

### Simulated decode rates

These come from `python3 -m sim.k6digi.ft8_sweep --path ...`, with SNR in 2500 Hz as WSJT-X
reports it.

**Clean audio into the decoder alone:**

| SNR (dB) | -16 | -17 | -18 |
| --- | --- | --- | --- |
| Decoded | 87% | 65% | 38% |

**Through the K6's GP0 path:**

| Path | -10 | -12 | -14 | -16 | -17 |
| --- | --- | --- | --- | --- | --- |
| K6 at the AGC's level (floor 0.05) | 95% | 92% | 77% | 18% | |
| K6 at floor 0.03 | | 62% | 10% | | |
| K6 with the gain set-up (floor 0.15) | | | 95% | 75% | 50% |
| Ideal front end (no sigma-delta) | | | | 82% | |

The floor is the receiver noise's level in the 1-bit path. It is the main unknown of the GP0
model, and PR #70's `gp0cap` noise pass is the bench run that measures it.

**Real recordings.** The decoder also reads ft8_lib's real WSJT-X test recordings, one 200 Hz
window at a time (`python3 -m sim.k6digi.ft8_wav FILE.wav`). On 110130 it found 4 of 5
signals; on 110145 and 110200 it found some of them.

**The `s=` figure is rough.** The SNR estimate is calibrated on the audio path. Through the K6
path it reads several dB low: for example, -19 for a -12 dB signal.

### Size and CPU

**Code sizes:**

| Part | Size |
| --- | --- |
| `ft8wf` | 3,822 B of code at -Oz, so the image is 3.8 KB of 4 KB |
| `ft8rx` | 7,234 B |

The largest functions in `ft8rx`:

| Function | Size |
| --- | --- |
| fk_row | 1.8 KB |
| k6_main | 1.4 KB |
| LDPC table FK_NM | 581 B |
| fk_text | 476 B |
| fk_ldpc | 432 B |
| callsign unpacking | 420 B |

**RAM:**

- `ft8wf`: about 2.3 KB of stack (measured in the emulator), with the 1 KB DMA ring on the stack.
  It needs no scratch.
- `ft8rx`: needs the ring (512 B), fk_t (~0.5 KB), the 162-row waterfall (nb = 24: 1.9 KB), the
  sync scores (0.1 KB) and the LDPC work (0.9 KB).

**CPU:** on the softmodem thread's calibrated M0 emulator (`tests/k6_live_emu.py --module`,
1.5 cycles an instruction, checked against gp0dma's measured idle on the K6), `ft8wf` waits for
the DMA about 30% of the time, with the stop-key scan costed at 60 us (see run 1 below). The
drain loop was tightened for run 2: a 64-sample count is four unrolled passes of four words,
the mixer's phase steps by one add a count, and the ring is 1 KB (186 us at 5.5 MS/s) instead
of 512 B. Counts of 128 samples would halve the per-count work, but their boxcar folds more of
the sigma-delta's noise onto the IF: in the sim, -16 dB fell from 75% to 38% (floor 0.15) and
from 42% to 5% (floor 0.06), so the counts stay at 64 (`LCNT` in gp0rx.h).
A decode in `ft8rx` adds a short burst; its pause is zero-filled like any other.

## ft8wf: what runs on the radio today

**Its serial lines:**

```
ft8wf start nb=32 f=1500 inv=0
ft8wf gain rms=4.7 g=03ff rms=9.1 r2=27 ks=5490     (noise rms at the AGC, at the gain chosen)
w 0 <44 base64 chars>                              (two 80 ms rows of 16 bytes, numbered)
ft8wf st t=15 lost=0 ks=5492 flo=812 idle=30       (every 15 s; lost = DMA ring overruns,
                                                    idle = % of the time waiting for the DMA)
ft8wf done rows=3750 lost=180
```

- **How long it runs.** It runs 180 s (FT8_SECONDS) or until a key, and returns the number of rows sent.
- **The serial rate.** One line every 160 ms is about 350 bytes/s on the report.
- **Errors it can return:**
  - -2: a key before the start.
  - -3: an old image.
  - -4: PB1 is not an input.
  - -5: REG_33 refused.
  - -7: TIM17 or DMA channel 1 is in use.
- **What it writes.** It writes REG_34[3:0] (code 13), REG_33 bit 8, REG_73 bit 4 (AFC off),
  and REG_7E and REG_13 (the gain). It also uses TIM17 and DMA channel 1 and their clocks.
  All of these are put back when it exits, and it is receive only.
- **Decoding on the PC.** `python3 tools/k6_ft8.py decode LOG` rebuilds the waterfall from the
  `w` lines and decodes it. Rows lost on the serial link become floor rows, so times stay
  right.
- **Making test audio.** `tools/k6_ft8.py wav OUT.wav --text "CQ AG7EW CN87" --f0 1480` makes
  12 kHz FT8 audio, a transmission every 15 s, to play into the IC-7100.

## Bench run 1 (2026-11-06): no decodes, the ring overran

Run on Jeff's bench through hamlet, with FT8 from the IC-7100 in PKTUSB on 440.850.

- **Offset: fine.** gp0dma read the carrier at IF 16,938 Hz, 11 Hz above nominal, so the audio
  went at `--f0 1468`. The xtalstep reading of 16,763 Hz (below) was wrong; gp0fm's 16,919 was
  close.
- **Noise floor: low.** `ft8wf gain rms=3.7 g=03ff rms=4.1`: the gain went to its top step and
  the counts' rms only reached 4.1, under the 8.0 the sim's floor-0.15 numbers assume. It is
  nearer the sim's floor 0.06, where -14 dB decodes 80% and -16 dB 42%.
- **The ring overran about 6,450 times a second** (`lost=1933930` over 300 s), and nothing
  decoded. The 3,748 rows reached the PC intact (0 lost on the link), but they held mostly
  zero-filled gaps.
- **The cause: the stop-key check.** The main loop called `api->key_down()` on every pass of
  the drain loop. Aura's `any_pressed` scans six keypad rows with a 5 us SysTick wait each. In
  the emulator, a 1 us key_down gives 0 overruns, 36 us gives 7 a second, and 60 us gives
  6,140 a second, which matches the radio. gp0dma never calls it inside its sampling loop,
  which is why its calibration held. Run 2's image checks the key once a row (every 40 ms),
  in `ft8wf` and `ft8rx`, and the emulator then shows 0 overruns at 60 us.

## Bench run 2 (2026-11-06): no overruns, but the tones missed the window

Run at 15:20 UTC with Jeff listening on another radio. FT8 went at `--f0 1468`, 60 s strong and
60 s 14 dB weaker, each followed by a true-CW ID (the 800 Hz `modem.k6 cw` WAV in PKTUSB).

- **The fix held:** `lost=0` in every st line for 300 s, `idle=39-40`, 3,748 rows, 0 lost on
  the link.
- **No decodes.** The 8 bins where the tones belong (1468-1512 Hz) were no stronger than in
  the quiet stretch. Only the window's outermost bins rose, and `flo` went from about 755 to
  882 while the strong burst was on air (run 1 showed the same 735 -> 853 step).
- **The suspect: a mirrored IF.** Nothing so far has shown which side of the IF the upper
  sideband lands on: gp0dma's `if=` reads a bare FM carrier. If it lands below, FT8 at 1468-1512
  Hz sits near 15,440 Hz, 2,980 Hz from ft8wf's mixer at 18,427 Hz. That is within a few Hz of
  the second CIC's 15th null (15 x 198.6 Hz), so almost nothing of it reaches the window, while
  the strong tone still lifts the 1-bit path's floor.
- **The test that settles it** (next run): gp0cap records the raw IF while the IC-7100 sends a
  steady 1000 Hz tone in USB (pass 1) and 2000 Hz (pass 2); `tools/k6_ft8.py tone LOG` finds both
  lines, so the side and the carrier's offset, and names the module (`ft8wf`, or `ft8wf_inv`
  built with FT8_INV 1) and the `--f0` that centre FT8 on the window. Then FT8 again,
  slot-aligned to the UTC 15 s boundaries so WSJT-X on another radio can decode it too.

ft8wf now runs 180 s by default (it was 300 s, more than a test needs).

## Bench steps

The run needs no keys on the K6 itself. It sends one module and plays FT8 from the IC-7100 in
USB on 440.850; Jeff OK'd PKTUSB on 440.850 on 2026-11-06 for the AF sweep. Bench rules: never
write to ttyUSB2, the CW ID keeper runs, and one thread is on the bench at a time.

1. **Find the K6's frequency offset.**
   - PR #71's xtalstep run measured a bare carrier at IF 16,763 Hz against the nominal 16,927 Hz.
     That is 164 Hz low, more than the ±100 Hz window.
   - The earlier gp0fm run read 16,919 Hz, so the two disagree. Settle it with a carrier and
     gp0dma's `if=`, or with the first `ft8wf` run below.
   - Part of the offset is known: Aura folds a +220 Hz crystal correction into REG_38/39 (the
     word reads 440,850,220 Hz). The rest is the IC-7100's own error; how the total splits
     between the two, and its sign, are unmeasured.
   - Then centre the audio on the window: `--f0 = 1480 + (16927 - IF)`, for example
     `--f0 1644` if the IF really is 16,763.
2. **Send the module from CI** (artifact `mod-ft8wf.bin`), with the IC-7100 in FM:
   `python -m modem.k6 mod ft8wf.wav mod-ft8wf.bin --api-level 2 --key-file ~/.config/sotto/k6-mod.key`,
   then play it as for any module, keeping the serial log.
3. **Play FT8 in USB.** When the log says `ft8wf start`, switch the IC-7100 to PKTUSB
   (`rigctl M PKTUSB 0`), key up and play
   `python3 tools/k6_ft8.py wav ft8.wav --f0 <step 1> --repeat 4` (60 s). Then key down and go
   back to FM.
4. **Decode:** `python3 tools/k6_ft8.py decode ~/k6-logs/<log>`.
   - If nothing decodes, `rows` dumps the waterfall. A tone outside the window means the offset
     in step 1 is wrong.
   - The `ft8wf gain` line shows whether the noise-filling gain engaged.
5. **Sweep the level** (optional). Repeat at lower IC-7100 power or MOD level, with the decode
   rate against `s=`.

## Getting ft8rx onto the radio

The whole decoder needs about 7.3 KB of code and about 4 KB of RAM besides the stack. These
are the options, none tried:

1. **A bigger module arena in the firmware.** For example, a dev image without the sotto
   decoder (about 8 KB free, from the modular-firmware memo), or modules loaded into the
   decoder's RAM when the pager is idle. This is the clean route.
2. **Code in SPI flash, tables in RAM.** The K6 can't execute from SPI flash, so this only
   moves the tables (FK_NM 581 B, FK_GEN 996 B, which isn't needed for RX). It saves too little.
3. **Smaller code.** fk_text could go, sending the 77 bits as hex for the PC to unpack (-0.9 KB).
   A narrower window (nb = 16) shrinks the waterfall. Even with all of that it is still over
   4 KB.
4. **The RAM at 0x20005560-0x20005FFF** (2.7 KB) may be unused by Aura. That is unverified, and
   probing it is risky, since RAM below Aura's region hangs boot.

## FT8 transmit (planned)

The plan is FT8 as audio FSK on FM, through the FD6818's tone oscillator. REG_71 is the tone's
frequency word, about 0.097 Hz a step (BK4819: f × 10.32444); REG_70 holds its enable and gain.
The module would do this:

- **Key up.** Call `tx_on(19000)` (level 3 API, PR #71).
- **Send the 79 tones.** Change REG_71 every 160 ms, stepping it in sub-symbol increments
  (every 1-2 ms) along WSJT-X's GFSK ramps (BT 2). The host computes the tones from the
  message and builds them into the module, at 79 bytes.
- **Identify.** Send AG7EW in CW with `tx_pa`, then key down. 12.64 s of FT8 plus about 4 s of
  CW at 20 wpm is under 20 s.
- **Timing.** The K6 has no UTC clock, so the module starts a fixed delay after the upload's
  carrier drops. The host schedules the upload so that the FT8 starts on a 15 s boundary.
- **Receiving it.** The IC-7100 in FM gives the PC the audio, and the PC decodes it with
  `ft8_wav` (or WSJT-X).

Prerequisites (from the "Transmit our own FSK from the K6" thread, 2026-11-06). That thread's
TX module work is on hold, and the REG_71 idea is untested (Insights #8), so these are open:

- **PR #71 on the radio.** The level 3 TX API builds, but no image with it is flashed yet.
- **The tone reaching the carrier.** `tx_on` writes REG_30 = 0xC1FA (mic ADC off, no AF DAC
  bit). Aura's own tone-TX state is 0xC3FA, so the tone may not reach the carrier as `tx_on`
  leaves it. REG_30 stays refused to modules. If 0xC1FA carries no tone, `tx_on` needs a tone
  option that sets that bit (a firmware change).
- **Phase continuity.** Is the tone's phase continuous across a REG_71 write? And what deviation
  does each step of REG_70's gain give?

A first test module would key for a few seconds with REG_70/71 stepping between two tones
while the IC-7100 records FM audio. Once that works, `ft8tx` is a small module, about 1 KB.

## WSPR (planned)

- **The mode.** WSPR is 162 4-FSK symbols of 0.683 s with 1.46 Hz spacing (6 Hz in all, 110.6 s
  long). It carries 50 bits under a K = 32, rate-1/2 convolutional code with a Fano decoder.
- **Receive fits the K6.** The front end is the same as FT8's, with a second decimation to
  about 12 Hz. Symbol energies take 162 × 4 bytes and the Fano decoder's state is small, so it
  fits RAM and CPU easily.
- **Drift is the problem.** The K6 has a plain 26 MHz crystal. At 440 MHz, 0.1 ppm is 44 Hz, and
  WSPR's decoder tolerates only a few Hz of drift over a transmission. So the first step is a
  drift measurement: a bare IC-7100 carrier with the IF frequency logged once a second for 10
  minutes from a cold start (gp0dma's `if=` already measures it).
- **Transmit needs a decision from Jeff.** The 110.6 s transmission is longer than the TX API's
  20 s limit. It would need Jeff's OK for a longer keying limit, on 440.850 with the CW ID. The
  same drift question applies.

## Files

- `c/k6/ft8/ft8k.[ch]`: the receiver core.
- `ft8k_tables.h`: ft8_lib's LDPC tables (MIT, Karlis Goba), generated by
  `tools/gen_ft8_tables.py`.
- `ft8k_host.c`: the host CLI (`iq`, `wf`, `counts`).
- `c/k6/mod/gp0rx.h`: the shared IF front end.
- `c/k6/mod/ft8wf.c`, `c/k6/mod/ft8rx.c`: the two modules.
- `sim/k6digi/`: `ft8ref.py` (the encoder, GFSK, unpacking), `gp0.py` with `gp0sdm.c` (the K6
  IF and the 1-bit path), `ft8_wav.py` and `ft8_sweep.py`.
- `tools/k6_ft8.py`: decode, rows and wav.
- `tests/test_k6_ft8.py`: the tests.
