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
- **On the radio (bench run 5, 2026-11-06): the K6 decoded 6 of 7 FT8 frames,** `CQ AG7EW
  CN87` at s=-16, including all three frames sent 14 dB down. The K6 listened; the IC-7100
  sent. Runs 1-4 found and fixed a ring overrun (a stop-key scan in the hot loop) and the chip's
  AFC chasing the tones (REG_73 bit 4 only takes hold after RXON). With the AFC off the K6 sits
  145 Hz low at 440.850, so the PC aimed the audio at `--f0 1624`. Drift was a few Hz a minute.
  See "Bench run 5". Since 2026-11-07 the modules read that offset from the K6's calibration
  record instead (written once by `ifcal`, docs/k6-modules.md "Calibration"), so the PC plays at
  the true audio frequency, `--f0 1480`.
- **FT8 transmit works on air (bench run 6, 2026-11-06): the K6 sent `CQ AG7EW CN87` itself and
  the IC-7100 decoded it at snr -14.** `ft8tx` (1.4 KB) sends one frame as audio FSK on FM from
  the chip's tone oscillator (REG_70/71, 5 ms GFSK steps), then AG7EW in MCW, in 16.6 s under
  the level 3 API's 20 s cap. `tx_on`'s REG_30 carries the tone: no firmware change.
- **WSPR (2026-11-07, Jeff: "finish out the WSPR beacon and try for receive"): built and
  emulated, not yet on air.** See "WSPR".
  - Beacon: `wsprtx` (2.3 KB with its screen) sends `AG7EW CN87 10` as audio 4-FSK on FM
    from the tone oscillator, with an MCW ID, in 117.4 s. That needs the module TX cap raised
    from 20 s to 120 s, which means a reflash. `wsprbcn` repeats it every 10 minutes on UTC's
    2-minute grid from a sync mark the PC keys. On FM the tones come from the FD6818's crystal
    at 1.5 kHz, so the carrier's drift at 440 MHz never reaches the decoder.
  - Receive: `wspriq` (4.0 KB with its screen) streams the IF's complex baseband (about
    199 Hz, +-99 Hz around 1500 Hz) to the PC, and `tools/k6_wspr.py decode` decodes it. The
    decoder searches drift (+-24 Hz over a frame), so a K6 drifting 5-20 Hz is fine. On an
    emulated M0 a whole frame through the 1-bit path decodes; on the host, -24 dB with 6 Hz of
    drift and lost lines decodes.
  - Screens: module API level 6 (new) lets both modules draw on the K6's display: countdowns,
    a TX progress bar, and a receive waterfall. See "Screens".

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
ft8wf start nb=32 f=1500 inv=0 cal=-16596              (cal = the IF offset applied, 0.01 Hz;
                                                    runs 2-6 printed afc= REG_73 here)
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

## Bench run 3 (2026-11-06): the AFC pulls the tone, the sideband is upright

gp0cap (which leaves the AFC as the pager has it) recorded the raw IF while the IC-7100 sent a
steady 1000 Hz tone in USB. Pass 2's 2000 Hz tone never ran: the 80 s tone outlasted pass 1's
100 captures, and pass 2's wait for the carrier to drop timed out (ret -6). Use about 45 s
tones with gp0cap.

- **Upright.** The first capture, 2.5 s after key-up, put the tone at 17,874 Hz, about
  16,927 + 1,000. A mirrored IF would have put it near 15,900.
- **The tone slid toward the nominal IF**: 17,874, 17,639 (8.8 s), 17,398 (22 s), 17,241 (35 s),
  17,172 Hz (43 s), smoothly across the gain steps. An exponential through those points settles
  at about 16,968 Hz with a time constant of about 30 s. That is the chip's AFC pulling the
  strongest signal (here the tone) to the centre of the IF, not either radio drifting.
- **That explains run 2.** ft8wf set REG_73 bit 4 (the guide's afc_disable), but run 2's FT8
  left the window within seconds, the way an active AFC would pull it: from +1,490 Hz the tones
  pass the window's lower edge (-100 Hz) after about 2 s. The chip latches REG_38 and REG_3B
  until RXON (xtalstep), and the AFC bit evidently too. ft8wf now sets the bit, then retunes to
  the same frequency (`api->tune`, which does RXON) before it starts, and prints REG_73 as
  `afc=` on its start line.
- **Next run:** ft8wf with a 30 s steady tone at 1,490 Hz first (`tools/k6_ft8.py track LOG`
  should show it holding one bin), then FT8 at `--f0 1468`.

## Bench run 4 (2026-11-06): the first FT8 decode on the K6

ft8wf (AFC off, then RXON; start line `afc=4692`) listened for 180 s with `lost=0` and
`idle=39-40`. From its waterfall the PC decoded **`CQ AG7EW CN87`** at 1475 Hz, s=-23, from
the third of four strong frames sent at `--f0 1468`. The other strong frames and the three
weak ones (14 dB down) didn't decode.

The steady 1,490 Hz tone before them didn't sit in bin 14 or 15. At its onset the strongest bin
was the top edge (b31), then the bottom edge, then it held at bin 2 and then bin 1 (1,406-1,412
Hz) for 27 s. The window's mean showed a comb of 4-bin period with its deepest null at bin 15.
The same tone through the simulator, and through ft8wf on the calibrated emulator (a 1-bit
stream of a 1,490 Hz USB tone), lands cleanly in bin 14, so the cause is on the radio. Two
candidates: (a) with the AFC truly off, the K6's own offset puts the tone about 100 Hz above
the window, where it aliases to the bottom edge (the AFC had hidden that offset in runs 1-3);
(b) the AFC is still pulling. Run 5 settles it with `gp0cap_afc`, gp0cap built with
`CAP_AFC_OFF` (the same AFC-off and RXON), recording the raw IF of a steady tone in each pass.
`tools/k6_ft8.py tone LOG --each` prints every capture's peak, so a slide shows.

## Bench run 5 (2026-11-06): 6 of 7 frames decoded, and the K6's true offset

The K6 only listened; the IC-7100 sent every tone and frame (USB audio on FM-D's carrier at
440.850, then the AG7EW CW ID).

**Part A, the raw IF with the AFC off.** `gp0cap_afc` (start line `r73=4692`, bit 0x10 set)
recorded the IF of a 1,490 Hz tone in pass 1 and a 1,990 Hz tone in pass 2, 90 clear captures
each. `tools/k6_ft8.py tone LOG --hz 1490 1990 --each` gave:

- Tone IFs 18271.6 Hz and 18767.0 Hz, slope +0.991, so the upper sideband sits above the IF
  (upright, no mirror).
- **Carrier at IF 16781.6 Hz, 145 Hz below the nominal 16927.1 Hz** (about -0.33 ppm, the K6
  and the IC-7100 together). The AFC had hidden this in runs 1-3, which is why run 4's tone
  landed at the window's edge. It matches the earlier xtalstep reading (-164 Hz).
- The tool's aim: `--f0 1624`.

**Part B, FT8 at `--f0 1624`.** ft8wf listened 180 s with `lost=0`, `idle=38-40` and
`afc=4692`. A 20 s tone at 1,646 Hz (f0 + 22) held bin 15 (1,493.8 Hz) for 14 s and then bin 14,
one bin of drift. The PC decoded six frames from the waterfall (`decode` and `decode --thr 10`
agreed):

| t (s) | f (Hz) | s (dB) | frame |
|---|---|---|---|
| 54.00 | 1462.5 | -16 | strong 2 |
| 69.04 | 1462.5 | -17 | strong 3 |
| 84.00 | 1462.5 | -16 | strong 4 |
| 114.40 | 1462.5 | -16 | weak 1 (14 dB down) |
| 129.44 | 1456.2 | -16 | weak 2 |
| 144.40 | 1456.2 | -16 | weak 3 |

All six read `CQ AG7EW CN87`. Strong frame 1 didn't decode. It started 3 s after the steady
tone ended, and `track` shows the K6's level still recovering then (t = 34.6-36.5 s). A gap of
a full slot after any steady tone avoids it. The weak frames decoding at the same s=-16 as the
strong ones says the K6's own path (the 1-bit IF, the 4-bit waterfall) sets that figure, not
the bench's RF level: both are strong signals at the antenna.

**Stability (the K6 and the IC-7100 together; this bench can't separate them):**

- Pass 1: the 1,490 Hz tone slid from 18277 to 18270 Hz over about 45 s, about 0.17 Hz/s,
  with ±1.3 Hz capture-to-capture jitter (the 2.6 Hz bin of the capture's FFT).
- Pass 2, 50 s later: the 1,990 Hz tone moved about 3 Hz over 45 s. The carrier estimate
  from pass 2 alone is 4.6 Hz below pass 1's.
- The FT8 run, about 2 minutes later: under about 6 Hz over 90 s (one decoder bin).
- For FT8 that is ample: a 12.6 s frame sees 1-2 Hz against 6.25 Hz tone spacing.
- For WSPR (110.6 s, 1.46 Hz spacing) it is marginal: these rates give 5-20 Hz over a frame,
  while wsprd tolerates a few Hz of linear drift. The slide slowed through the session, so a
  warmed-up pair may do better. It needs a long run (see "WSPR").

**Module uploads lost chunks.** In runs 4 and 5 many of each upload's packets came in with
`K6 MOD r=-1`, which is `KM_BAD_CRC` (c/k6/k6_mod.h): the packets arrived damaged. The
`xfer=` on those lines is the last transfer that checked, since a bad packet doesn't change it,
so it only looked as if the K6 filed them under the old transfer. gp0cap_afc got 6 of 22
chunks at first and ft8wf 32 of 33 (chunk 2 missing). A resend of the missing chunks with
`--only N --serial S --xfer X` always completed it, at the cost of a second keying. The
cause (link margin, or the receive set-up after a GP0 module) is open.

## Bench steps

The run needs no keys on the K6 itself. It sends one module and plays FT8 from the IC-7100 in
USB on 440.850; Jeff OK'd PKTUSB on 440.850 on 2026-11-06 for the AF sweep. Bench rules: never
write to ttyUSB2, the CW ID keeper runs, and one thread is on the bench at a time.

1. **Find the K6's frequency offset.** On a level 4 image the K6 keeps it in its calibration
   record (SPI flash 0x10F000, `c/k6/mod/k6_cal.h`), which `ifcal` writes once (docs/k6-modules.md,
   "Calibration"; Jeff's K6 has one since 2026-11-06 20:51: -165.8 Hz). ft8wf, ft8wf_inv and
   ft8rx move their mixer by it, and the start line says so: `ft8wf start ... cal=-16596`
   (0.01 Hz; `cal=0` means no record or an image below level 4). With a record, play at
   `--f0 1480` (the default). Without one, aim the audio by hand: run 5 found the carrier at
   IF 16,781.6 Hz, 145 Hz low, which gave `--f0 1624`. To measure that way (another radio, a
   cold K6), send `gp0cap_afc`, play 48 s tones at 1,490 and 1,990 Hz in PKTUSB, and run
   `python3 tools/k6_ft8.py tone LOG --hz 1490 1990 --each`. It prints the carrier and the
   `--f0` to use. The AFC must be off (ft8wf and gp0cap_afc do that, then RXON), or the chip
   pulls the tones toward the IF centre over about 30 s.
2. **Send the module from CI** (artifact `mod-ft8wf.bin`), with the IC-7100 in FM:
   `python -m modem.k6 mod ft8wf.wav mod-ft8wf.bin --api-level 2 --key-file ~/.config/sotto/k6-mod.key`,
   then play it as for any module, keeping the serial log. If `K6 MOD r=-1` lines (bad CRC)
   leave chunks missing, resend them with `--only N --serial S --xfer X`.
3. **Play FT8 in USB.** When the log says `ft8wf start`, switch the IC-7100 to PKTUSB
   (`rigctl M PKTUSB 0`), key up and play
   `python3 tools/k6_ft8.py wav ft8.wav --f0 <step 1> --repeat 4` (60 s: 1480 with a record). Then key down and go
   back to FM with `rigctl M PKTFM 12000`. A passband of 0 selects the IC-7100's 6 kHz filter,
   too narrow for the module uploads (found by the softmodem thread, PR #81). Leave a full
   15 s slot between any steady tone and the first frame.
4. **Decode:** `python3 tools/k6_ft8.py decode ~/sotto-work/logs/<log>`.
   - If nothing decodes, `rows` dumps the waterfall. A tone outside the window means the offset
     in step 1 is wrong (or, with `cal=` set, the record is stale: run `ifcal` again).
   - The `ft8wf gain` line shows whether the noise-filling gain engaged.
5. **Sweep the level** (optional). Repeat at lower IC-7100 power or MOD level, with the decode
   rate against `s=`.

## The DMA rate moves with the CPU loop (for when FT8 resumes)

The softmodem thread found that DMA channel 1's flat-out rate depends on what the CPU is doing,
from 5,490 to 5,875 kS/s by loop: the bus is shared. ft8wf and ft8rx handle most of this. The
mixer phase and the row clock both come from TIM17 time, and `rx_rate` re-measures the
cycles a transfer (`cps8`) after every row. One thing is fixed at set-up, though.

- `rx_gain` picks the second CIC's decimation `r2` (about 200 Hz out) from the rate measured
  during the gain loop. The run loop is a different loop, so its rate differs. Run 5 shows
  `ks=5488` at set-up against 5,561-5,582 while running, +1.5%.
- So the decimated rate runs 1.5% fast, and the 32-point DFT's bins are 6.34 Hz, not 6.25 Hz.
  Across FT8's 8 tones that's 0.7 Hz, a tenth of a bin, which run 5 shrugged off. At the
  softmodem thread's worst case of 7%, tone 7 would sit half a bin off, costing decodes.
- **Fix when FT8 resumes:** re-pick `r2` (or scale the DFT's bin step) from `cps8` after the
  first second of the run loop, or measure the set-up rate inside the run loop itself. Check
  that `ks=` on the `st` lines matches the `gain` line's within 1%.

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

## FT8 transmit: `ft8tx` (works on air, bench run 6)

`c/k6/mod/ft8tx.c` (1.4 KB, API level 3) makes the K6 send one FT8 frame itself, as audio FSK
on FM from the FD6818's tone oscillator, then identify. The IC-7100 in FM receives it, and the PC
decodes the IC-7100's audio. What the module does:

- **Checks first.** It keys up only when the pager listens on 440.850, and only after the
  channel has been clear (below -90 dBm) for 1 s, so the upload's sender has unkeyed. It gives
  up after 30 s of a busy channel (-7).
- **Keys up** with `tx_on` for the frame, the ID and a margin: 300 + 12,640 + 400 + 3,120 +
  1,000 ms, about 17.5 s, under the core's 20 s cap. FM, low power, no CTCSS, mic ADC off.
- **The frame.** After a 300 ms lead-in (carrier, no tone) it turns tone 1 on (REG_70 = 0x8000 |
  60 << 8, Aura's tone gain) and writes REG_71 every 5 ms: tone k at 1,500 + 6.25 k Hz, 160 ms a
  symbol, with WSJT-X's GFSK (BT 2) transition over the 30 ms either side of each boundary (a
  six-entry step table). REG_71 is f x 10.32444, so one tone step is 64.53 words and the
  resolution 0.097 Hz. Timing is the module clock, from the M0's 16 MHz crystal and PLL.
- **The ID.** 400 ms after the frame it sends AG7EW in MCW at 20 wpm, keying the 800 Hz tone
  with REG_70, then keys down. A key press stops it at once (-4); if the core's limit keys it
  down first, it returns -5.
- **The message** is built in: `c/k6/mod/ft8tx_msg.h`, written by
  `tools/k6_ft8.py txhdr --text "CQ AG7EW CN87" --out c/k6/mod/ft8tx_msg.h` from the same
  encoder the receiver tests use. Another message means a rebuild (CI does it).

`tests/test_k6_ft8tx.py` runs it on the emulated M0: 2,528 REG_71 writes 5 ms apart, a
12.64 s frame, each symbol's middle on its tone within 0.15 Hz, the MCW reading AG7EW, nothing
written but REG_70/71 and both left at 0, and every way out keying down. Played back as audio,
the REG_71 words decode to `CQ AG7EW CN87` at 1,500 Hz with the K6's own receiver.

**Bench run 6 (2026-11-06, 18:57 UTC): the K6 sent FT8, and the IC-7100 decoded it.** The
IC-7100 sent the upload (12 chunks, one pass, one r=-1 line), then only listened, in FM-D with
the squelch open, recording its USB audio at 48 kHz. The K6 waited for the clear channel and
keyed up for 16.6 s (`K6 MOD tx on hz=440850000 max=17460 ms`, then `ft8tx sent sym=79
ms=12640 id=52`, `ret=79`).

- **Decode:** `python3 -m sim.k6digi.ft8_wav rec6.wav` gave `CQ AG7EW CN87` at 1500 Hz, snr -14,
  the same over 200-3000 Hz.
- **Tones:** the strongest peak in each 160 ms symbol reads back all 79 tone indices exactly,
  1500.0-1543.8 Hz, with the three Costas arrays at symbols 0, 36 and 72.
- **Levels (dBFS):** frame -15.5 RMS with the tone at -28 and its 2nd harmonic at -61; the
  300 ms lead-in and the 400 ms gap are carrier quieting only (-36, -67); the MCW ID is 800 Hz
  at -18, 64 dB over the median, with clean keying.
- **So the tone reaches the carrier with `tx_on`'s REG_30 = 0xC1FA.** The AF DAC bit (9) that
  Aura's tone transmitters set isn't needed for TX; it is probably the sidetone to the speaker.
  No firmware change. Phase continuity across REG_71 writes is good enough for a clean decode.
- **`tx_on` blocks about 1.4 s** (Aura's `enter_tx`: band switch, PLL, PA): the RF came up 1.4 s
  after the `tx on` line, and the module's 300 ms lead-in ran after that, as designed, since it
  times from `tx_on`'s return. The 17.46 s limit held.
- The IC-7100's USB audio stays muted for about 1.8 s after its own unkey, so a recording
  misses the K6's first 1.8 s if the K6 keys up sooner. The 1 s clear wait plus `tx_on`'s 1.4 s
  covers it.

**Still open:** a level sweep (FT8_GAIN against the IC-7100's audio and the decode margin),
weak-signal reception at a distance, longer messages than CQ (a rebuild each), and timing the
key-up to a 15 s UTC slot for WSJT-X (the host would schedule the upload).

**Bench steps** (the K6 transmits; the IC-7100 only receives):

1. IC-7100 in FM on 440.850, its USB audio recording (`arecord` at 48 kHz) from before the upload.
2. Send `mod-ft8tx.bin` from CI: `python -m modem.k6 mod ft8tx.wav mod-ft8tx.bin --api-level 3
   --key-file ~/.config/sotto/k6-mod.key`, play it on the IC-7100, and unkey. Resend any
   missing chunks with `--only`.
3. The K6 waits for the channel to clear, keys up for about 17 s (`K6 MOD tx on`), and says
   `ft8tx sent sym=79 ms=12640 id=52`.
4. Decode the recording: `python3 -m sim.k6digi.ft8_wav REC.wav --lo 1300 --hi 1800`.

## WSPR

WSPR is 162 4-FSK symbols of 8192/12000 s (0.683 s) with 1.46 Hz spacing, 6 Hz wide and
110.6 s long. It carries 50 bits (a callsign, a 4-character grid, power in dBm) under a K = 32,
rate-1/2 convolutional code, interleaved by bit reversal. Frames start 1 s after an even UTC
minute.

**Reference** (`sim/k6digi/wsprref.py`, MIT): the encoder (pack, the code, the interleave and
the sync vector, checked against the protocol's published constants) and a decoder. The decoder
removes a linear drift (tried from -24 to +24 Hz over the frame in 1 Hz steps), then searches
start time (quarter symbols) and frequency (half tones) with the sync vector. It takes soft
symbols from the best candidates and runs a Fano sequential decoder in C
(`c/k6/wspr/wspr_fano.c`, built for the host through ctypes). It decodes clean audio at -29 dB
in 2500 Hz, and through 8, 12 and -15 Hz of drift, in about a second.

**Wander tracker** (added after the first bench receive, 2026-11-07, which streamed cleanly but
wandered about +-1.8 Hz within the frame, more than the 1.46 Hz tone spacing, so the linear
search found nothing). It now runs first: each symbol's spectrum, padded so that a tone
spacing is 16 bins of 0.092 Hz, scores every tone-0 frequency by the sync vector's parity (the
larger of the two tones the sync bit allows, less the larger of the two it doesn't), and a
Viterbi search finds the best track that moves at most 2 bins a symbol (0.27 Hz/s). The parity
score is what keeps the track from slipping by a tone. The signal is derotated along the track
and decoded as if it stood still. On simulated slow random wander of +-1.8 and +-3 Hz it
decodes at -24 dB (the linear search: not at all at +-1.8 Hz, even at 0 dB); with no wander it
matches the linear search (-27 dB 4/4, -29 dB 1/4), with no false decodes on noise, in 0.2 s.
The linear search stays as the fallback for drift faster than 0.27 Hz/s.
The scores are plain power over the median: on the bench's +37 dB stream both a clip and log
power lost the track to the skirt the K6's path puts around a strong signal. And since a
strong signal also decodes from a neighbouring 40 Hz window (through the resampler's skirt,
25 Hz off), `tools/k6_wspr.py decode` keeps each message's most central decode.

### Beacon: `wsprtx`, `wsprbcn` (level 3)

- **How it sends.** The same tone oscillator path as `ft8tx`: REG_70 = enable | gain,
  REG_71 = f x 10.32444, one write a symbol (no GFSK: WSPR is plain CPFSK and the
  oscillator is phase continuous). Symbol k starts at t0 + k x 2048/3 ms, kept in thirds of a
  ms, so 162 symbols land on 110.592 s. The MCW ID follows, then key-down.
- **TX length.** `tx_on(117,412 ms)`: the 300 ms lead, the frame, a 400 ms gap, the 3.1 s ID and
  a 3 s margin (a longer lead when `tx_on` returns quickly). The module TX cap in the Aura patch
  (`MOD_TX_MAX_MS`) goes from 20 s to 120 s; an older image keys it down at 20 s (-5).
- **Drift doesn't matter on FM.** The receiver hears audio tones made from the FD6818's
  crystal, divided down to 1.5 kHz: a 2 ppm error moves them 0.003 Hz. The 440 MHz carrier
  can drift 20 Hz and the FM discriminator never sees it.
- **Timing: the K6 has no clock.** Aura has only uptime: TIM14 in 100 us ticks from the 16 MHz
  crystal and PLL, which modules see as `api->ms()`. The MCU (KD32F328) has an RTC block, but
  Aura doesn't start it and the board shows no 32 kHz crystal or backup cell, so nothing keeps
  time with the power off. So `wsprbcn` takes UTC from the PC:
  `tools/k6_wspr.py sync` keys the IC-7100 (through `k6_bench.py`'s PTT lock) and plays the
  bench's CW ID (about 9 s), unkeying by the PC's NTP clock 10 s before a slot. The mark is the
  ID itself: Jeff's rule is that every test transmission carries AG7EW's ident, with no bare
  carriers, and a separate ID first would be a mark of its own at the wrong time. The module waits up to 150 s for a
  carrier of at least 0.5 s, timestamps its drop (polled every 5 ms), and starts the first
  frame's tones 10 s later. Each slot's channel check and key-up come 3.3 s before its tones.
  Later frames follow every WSPR_EVERY x 2 minutes on the crystal, which holds the grid to a few
  ms an hour. With no mark it says so and starts at once. `wsprtx` (one frame, no sync) starts
  3.3 s after the upload's sender unkeys.
- **Lines:** `wspr AG7EW CN87 10 f=1500 g=60 n=6 rx=440850000`, `wspr sync: waiting for the
  mark`, `wspr next 10 s`, `wspr sent 1 sym=162 id=52`, `wspr busy, slot skipped`.
- **Tests** (`tests/test_k6_wsprtx.py`, emulated M0): one frame's REG_71 words played as audio
  decode with the reference; 162 symbols at 682.67 ms; the beacon keeps the grid (240 s apart
  with WSPR_EVERY 2); the sync mark puts the first tones 10 s after the drop (within 20 ms);
  no mark gives the fallback; refusals; key-down on a key or the old 20 s cap.

### Receive: `wspriq` (level 2) and `tools/k6_wspr.py`

The whole decoder needs the whole frame (162 x 4 tone powers) and a Fano decoder over a K = 32
code: far past the 4 KB store. So the K6 does what `ft8wf` does for FT8: the front end on the
radio, the decode on the PC.

- **On the K6.** `gp0rx.h`'s front end (GP0 by DMA, the gain set-up, the real-time mixer at the
  IF + 1500 Hz with the calibration record's offset, two CICs) gives complex samples at about
  199 Hz. The module sends them as they are: 15 samples a line (I and Q as signed bytes with one
  shift for the line) and the 96 MHz time of the line's last sample, 53 characters, about 13
  lines a second (ft8wf's rate). 3.4 KB of code, 4.0 KB with the level 6 screen.
- **Lines:** `wspriq start f=1500 inv=0 n=15 cal=-16580`, `wspriq gain ...` (as ft8wf),
  `q 1a2b3c4d 3 <40 base64 chars>`, `wspriq st t=15 lost=0 ks=5492 idle=61`,
  `wspriq done n=3200 lost=0`. It runs 240 s (two slots, so a frame started anywhere in the first
  two minutes is caught whole) or until a key.
- **On the PC.** `tools/k6_wspr.py decode LOG` places each line by its time (lines are a whole
  number of 15-sample blocks apart, so a lost line becomes 15 zeros), fits the sample rate over
  all the lines (the DMA's rate moves a few % with the CPU's load), mixes, resamples to 375/8 Hz
  and runs the reference decoder. By default it searches +-50 Hz around 1500 Hz in three 40 Hz
  windows. It prints the audio frequency, start (s into the stream), drift and the message.
- **Drift.** In USB the K6 sees the carrier's drift directly: run 5 measured 0.05-0.17 Hz/s,
  5-20 Hz over a frame. The first bench receive (2026-11-07) also wandered about +-1.8 Hz
  within the frame, which the linear search can't follow; the wander tracker (above) does.
- **Tests** (`tests/test_k6_wspriq.py`): a steady tone on the emulated M0 lands at +10 Hz with
  the rate fitted to 190-215 Hz; the calibration record moves the mixer; the host decodes
  -24 dB with 6 Hz of drift and 20 lost lines, and a signal 37 Hz off centre through the CLI.
  The whole chain (WSPR at the IF through the 1-bit path, the emulated module, its lines, the
  host decoder) decodes too; it takes 3.5 minutes, so it is opt-in (`K6_SLOW=1`).

### Bench steps

The image must have the 120 s module TX cap (a `tools/k6_update.py` reflash, one time).

**Beacon** (the K6 transmits WSPR in FM; the IC-7100 receives):
1. IC-7100 in FM on 440.850, its USB audio recording from before the upload.
2. Send `mod-wsprbcn.bin` (or `mod-wsprtx.bin` for one frame) with `python -m modem.k6 mod`.
3. `python3 tools/k6_wspr.py sync`. The K6 says `wspr next 10 s`, keys up 3.3 s before the slot,
   and sends for 117 s (`wspr sent 1 sym=162 id=52`).
4. `python3 -m sim.k6digi.wsprref decode REC.wav`. WSJT-X on the IC-7100's audio should decode
   it too, with dt near 0 if the sync held.

**Receive** (the IC-7100 sends WSPR in USB; the K6 receives):
1. IC-7100 in USB on 440.850. Make the audio: `python3 tools/k6_wspr.py wav wspr.wav --f0 1497`.
2. Send `mod-wspriq.bin`, then play `wspr.wav` through `k6_bench.py send` within the next 2
   minutes. The K6's log fills with `q` lines for 4 minutes.
3. `python3 tools/k6_wspr.py decode K6LOG`.

**Real signals.** `wspriq` only listens, so it can sit on 144.489 MHz (2 m WSPR, USB dial) with
a matching `rx_hz`, any time, without asking.

### Screens (API level 6)

Modules couldn't draw before; the screen was Aura's. Level 6 (docs/k6-modules.md "API level 6")
adds `lcd_text`, `lcd_bar`, `lcd_wf` and `lcd_flush`, after level 5's flash calls. Both WSPR
modules use them only when `api->level` is 6 or more, so the same builds run on older images
without a screen. Frames the emulated modules drew:
`/mnt/project-files/k6-wspr/wspr-screens-emulated.png`.

- **Beacon:** the title (`WSPR BEACON  440.850`, or `WSPR TX` for one frame), the message, then
  by phase: waiting for the sync mark with its 150 s countdown (no redraws while a mark is up,
  so its drop stays timed to 5 ms); `next slot in 7:58`, redrawn each second; `TX` with a bar
  over the 162 symbols, every 4 symbols (2.7 s); `ID AG7EW`. Below: frames sent, whether it is
  on the UTC grid, and "hold a key to stop". 2.3 KB (`wsprbcn` 2.8 KB).
- **Receive:** `WSPR RX  440850 kHz`, the window and calibration (`1500Hz +-99  cal -166`), a
  24-row waterfall (0.64 s a row, 15 s in all) and the time and lost lines. Each 40 ms row
  takes a 32-point DFT of the newest 32 samples (6.2 Hz bins, 4 px each, centre at x = 64),
  keeping each bin's largest power; every 16 rows the bins become a pixel row, lit from the
  middle out at 2, 4, 8 and 16 times the row's mean power. ft8k's own DFT rows would cost
  1.3 KB more, which doesn't fit. Each flush (every 1.3 s) is a 20-40 ms gap the stream
  zero-fills. `wspriq` is 4,070 B: two bytes under the limit.

## Files

- `c/k6/ft8/ft8k.[ch]`: the receiver core.
- `ft8k_tables.h`: ft8_lib's LDPC tables (MIT, Karlis Goba), generated by
  `tools/gen_ft8_tables.py`.
- `ft8k_host.c`: the host CLI (`iq`, `wf`, `counts`).
- `c/k6/mod/gp0rx.h`: the shared IF front end.
- `c/k6/mod/ft8wf.c`, `c/k6/mod/ft8rx.c`: the two receive modules; `gp0cap_afc.c`, the raw-IF
  capture with the AFC off.
- `c/k6/mod/ft8tx.c` with `ft8tx_msg.h`: the transmit module and its tones.
- `c/k6/mod/wsprtx.c` with `wsprtx_msg.h`, `wsprbcn.c`: the WSPR beacon; `wspriq.c`: WSPR receive.
- `c/k6/wspr/wspr_fano.c`: the Fano decoder (host).
- `sim/k6digi/`: `wsprref.py` (WSPR's encoder and decoder), `ft8ref.py` (the encoder, GFSK, unpacking), `gp0.py` with `gp0sdm.c` (the K6
  IF and the 1-bit path), `ft8_wav.py` and `ft8_sweep.py`.
- `tools/k6_ft8.py`: decode, rows, wav, tone, tonewav, track, txhdr and wsprhdr.
- `tools/k6_wspr.py`: decode, iq, wav and sync.
- `tests/test_k6_ft8.py`, `tests/test_k6_ft8tx.py`, `tests/test_k6_wsprtx.py`,
  `tests/test_k6_wspriq.py`: the tests.
