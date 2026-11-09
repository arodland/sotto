# AIS on the K6: ship traffic as a clock

*2026-11-09.* The APRS clock works (docs/k6-aprs-clock.md), but no RF station near Lynden sends
APRS timestamps. Jeff asked which other signals in the K6's range (18 to 630 MHz) carry time, and
chose AIS from the list. Ships and shore stations on 161.975 and 162.025 MHz send time all day,
and the Salish Sea is 15 to 25 km from the bench.

**Status: the K6 sets its own clock from AIS (run 3, 2026-11-09 04:10Z).** Over 15 minutes,
receive only, `aisclk` decoded 52 frames from a US shore base station (MMSI 003669705) on the
radio. It had the second 10 s in and slot-locked fixes from the third minute. Every burst it timed
sits on the AIS slot grid to about 0.1 ms. The K6's own clock is the weak part: it runs about 134
ppm fast and wanders ±100 ppm over minutes, so the module snaps to the slot grid on every burst,
not only those that carry a slot number. With that, run 4 held each burst's millisecond to ±1 ms
for 14 minutes (54 fine fixes from 55 frames). Runs 1 and 2 recorded bursts for the host. Run 2
decoded the same station's date and second, matching NTP.

Jeff (2026-11-09 03:34-03:43Z): BLOWFANG needs both this and the APRS clock. AIS goes first,
because it's the more reliable where it's heard, and the K6 listens to APRS only when no AIS
turns up (below, "One clock from AIS and APRS").

## What AIS carries

AIS (ITU-R M.1371-5) is 9600 bit/s GMSK (BT 0.4, ±2400 Hz) in self-organised TDMA. A UTC
minute is 2250 slots of 26.67 ms, and slot 0 starts on the minute. The clock can use three
things:

| What | Where | Resolution | How often |
|---|---|---|---|
| Year, month, day, hour, minute, second | message 4 (base station report), 11 | 1 s | every 10 s from each shore base station |
| UTC second of the report | messages 1, 2, 3 (class A), 18 (class B) | 1 s | every 2 to 10 s from each moving ship |
| UTC hour and minute | SOTDMA comm state, slot time-out 1 (messages 1, 2, 4, 11, 18) | 1 min | about one report in 8 from each station |
| Slot number of this burst | SOTDMA comm state, time-outs 2, 4, 6 | **one slot, and the burst's own start to the bit** | about 3 reports in 8 |

The last row is the interesting one. A station on UTC (sync state 0) starts its burst on the slot
boundary, so the start flag of a burst sent in slot n begins at `n × 60/2250 s + 32 bits` into
the minute. Hearing that flag gives UTC within the minute to well under a millisecond, plus the
propagation time (about 70 µs at 20 km). The second or hour/minute fields of the same station
place the minute. That's far tighter than APRS's 1 s stamps, and enough to slot-align WSPR and
FT8 with no bench mark.

## The K6's receive path

```
GP0 code 13 on PB1 -> DMA ch1 ring (1 KB, ~5.5 MS/s) -> counts of 64 (~85.8 kHz)   (as dmrcap, aprsclk)
 -> mixed down by the nominal IF (16.927 kHz) -> 2nd-order CIC, decimate by 3 (~28.6 kHz, ~3 a bit)
 -> phase step a sample (the FM discriminator), its mean over the burst taken off (carrier offset)
 -> phase advance over each bit = the bit, at 8 clock phases and the DMA's rate ±0.15%
 -> NRZI -> HDLC (flags, unstuffing) -> CRC-16/X.25 (residue 0xF0B8) -> each byte reversed -> fields
```

A burst is 27 ms, so the receiver records it and decodes after. It tries each clock phase and
keeps any frame with a good CRC, which is simpler than a bit clock that has to lock within the
24-bit training sequence. The parser is checked against gpsd's sample sentences
(`tests/test_k6_ais.py`).

Decodes against C/N0 on the GP0 model (`python3 -m sim.k6if.ais pd`, message 4, IF offset ±500 Hz,
30 bursts each):

| dB-Hz | 62 | 59 | 57 | 55 | 53 | 51 |
|---|---|---|---|---|---|---|
| decoded | 30 | 28 | 25 | 16 | 1 | 0 |

That's about where a commercial AIS receiver sits: the standard's -107 dBm with a 7 dB noise
figure is 60 dB-Hz. The K6's GP0 path loses more. On DMR, a carrier the frozen-gain RSSI read at
-79 dBm looked like 48 dB-Hz on the model (docs/k6-dmr.md). If AIS maps the same way, the K6
needs bursts at about -72 dBm on that RSSI. That's an estimate from one other signal; aiscap
measures it.

Things tried that didn't help on the model: one-bit differential detection in place of the
discriminator (the same within a burst or two in 16), a 4-6 kHz FIR after the CIC (the chip's filter and
GMSK's own spectrum leave nothing to gain), decimating by 2 (too much of the sigma-delta's noise)
or by 4. Two that might: flipping the one or two least certain bits when the CRC fails (about 1
dB in other AIS receivers), and a coherent GMSK receiver on the complex baseband.

The start flag's time comes out about 0.6 bit (60 µs) late on the model, from the chip filter's
and the CIC's delay. That's a constant, to be calibrated once.

## aiscap: recording what the K6 hears

`c/k6/mod/aiscap.c` (level 2, receive only, about 2.7 KB of code plus a 3,600-count ring in the
scratch, so it needs a voice image's 11,776-byte arena; under 600 B of stack) freezes the gain at
REG_13 = 0x03FF (the maximum), alternates channel B and A each minute, and records a burst whole:
12 ms from before it was noticed, and the 27 ms burst after. The dumps are in gp0cap's format
plus an `e` line, and `python3 -m sim.k6if.ais cap LOG` decodes them.

**Version 2 (after run 1) polls the IF.** The MCU reads PB1 256 times a count in straight-line
Thumb, as `gp0fm` does (22.9 M samples/s, a count every ~11.5 us). Sampling the 6.5 MHz 1-bit
stream 4x faster than the DMA's 5.5 MS/s folds far less of its noise into the IF band: run 6 of
docs/k6-fd6818-audio.md measured 60 Hz rms of FM noise on a carrier against 166. On the model
with run 1's levels (receiver noise under the 1-bit path's, floor 0.01), message 4 decodes 12
to 14 times in 16 at 60 dB-Hz polled, and never at 5.5 MS/s.

The CPU is busy polling, so it can't read the RSSI while listening: an FD6818 read stalls the
polling ~50 us, a hole in the burst. Instead each count goes through a band-pass around the IF
into a leaky energy, and a burst is the energy 4.8 dB over the floor. Each turn starts with 0.37
s on the channel to measure the count rate and the floor (the quietest of 32 blocks). Only bursts
6 dB over the floor are dumped, so the 60 captures aren't spent on the weakest. The counts
before and after the trigger come at rates 0.3-0.4% apart (two paths through the loop), so
TIM17 counts the cycles of each and the host puts them on one grid (`ais.uniform`). The
emulator can't poll 23 M reads a second, so the test build (`AISCAP_EMU`) reads one count a
time, with the polling's time charged per read.

## Run 1, 2026-11-09 02:48-02:58Z (DMA version, af064d6 vlrh, big antenna, receive only)

The explorer thread handed over the bench. The upload went through on the first send, and RF
POWER read 0 before and after. The log is `tests/data/k6/air/aiscap-20261109T0247Z.log.gz`.

| minute | ch | floor dBm | bursts over floor + 8 dB |
|---|---|---|---|
| 1 | B | -105 | 54 |
| 2 | A | -103 | 50 |
| 3 | B | -106 | 127 |
| 4 | A | -106 | 110 |
| 5 | B | -104 | 62 |
| 6 | A | -103 | 54 |
| 7 | B | -106 | 123 |
| 8 | A | -103 | 53 |
| 9 | B | -104 | 48 |
| 10 | A | -103 | 58 |

- **AIS is there.** The captures show bursts 24-28 ms long, centred 1.3 kHz under the nominal
  IF (the K6's crystal at 162 MHz), with 90% of their power in ~8 kHz: GMSK 9600. The strongest
  peaked at -78 to -80 dBm, about one every 17 s on B.
- **None decoded.** The best bursts stand 6-8 dB over the noise in a 20 kHz band, about 50
  dB-Hz. The receiver needs 55 or more.
- **The noise is the tap's, not the band's.** The counts' spectrum is flat from 0 to 43 kHz
  with no carrier, with no hump at the IF: the receiver's own noise, at maximum gain, sits under
  the 1-bit path's. So a quieter spot wouldn't help, but more signal (antenna height) or less
  tap noise (polling) would.
- **Two bugs.** The `ks` rate overflowed 32 bits over a minute-long listen (it read ~3930;
  the real rate was ~5490), and the first 60 bursts, mostly weak, filled the captures in the
  first 66 s. Both are fixed in version 2.

## Run 2, 2026-11-09 03:16-03:26Z (polled version 0fee6c8, af064d6 vlrh, big antenna)

Same set-up as run 1, with RF POWER 0 before and after. The log is
`tests/data/k6/air/aiscap2-20261109T0315Z.log.gz`, and `tests/test_k6_ais.py` decodes it.

- The count rate was 81.85 kHz on every turn (±5 Hz), below the 89 kHz of 256 reads at 22.9
  MS/s: the loop's own cycles and Aura's interrupts. After the trigger it ran at 80.87 kHz (c1).
- 371 bursts over the trigger, 60 dumped by 167 s (the threshold was then 6 dB; it's 11 dB now).
- **10 frames from 9 captures, all from MMSI 003669705** (a US base station, 00366 prefix): 7
  message 4 base station reports and 3 message 20 (data link management). Every capture 12 dB or
  more over the floor decoded at least once. Nothing at 10 dB or less did.
- The reports come every 20 s on each channel, at :00, :20 and :40. Their UTC second matches the
  NTP-stamped serial log every time. The log line comes ~0.9 s late, which is the dump.
- **The slot numbers agree with the stamps.** Slot 784 is 20.907 s into the minute (the :20
  report), 1534 is 40.907 s (:40), and 32 is 0.853 s (:00). Two slot-stamped bursts 100 s apart
  put the K6's module clock at the same UTC offset within 2.3 ms. That's about 1 ms of `ms()`
  resolution plus 23 ppm of the K6's crystal over 100 s.
- Near the threshold, each start point gives a slightly different carrier-offset estimate and CIC
  phase, so trying four starts just before the trigger decodes more (`ais.receive_cap`: 10 frames
  against 6-8 for any one start).
- Something sends a burst every 4.000 s exactly on both channels, 7 dB (B) and 9 dB (A) over the
  floor. It never decodes, and it's probably local to the bench. The 11 dB dump threshold leaves
  it out.

## aisclk: the clock on the radio

`c/k6/aisrx.h` is the receiver in integers, for the M0 and the host alike: the mixer on a
64-entry sine, CIC/3, an 11-step CORDIC for the phase and magnitude, the mean phase step over
the samples above half the mean magnitude taken off (the carrier's offset), then at each of 8
clock phases the sign of the phase advance over a bit, NRZI and a streaming HDLC with the CRC.
It runs from four start points 32-256 counts before the trigger, about 20 ms of CPU a burst, in
a 1.9 KB phase array next to the ring. On run 2's captures (`c/k6/aisrx_host.c`, ctypes) it
decodes 9 of the host's 10 frames (the tenth comes only from the host's whole-capture grid), and
puts each start flag within a count (12 us) of the host's.

`c/k6/mod/aisclk.c` (level 2, 5.8 KB at -Oz; screen at 6, LED at 7). Its ring is 2,960 counts:
384 before the trigger and the burst's 2,576 after. With the phases and frames that's 5,044 B,
which fits the voice images' arena with the 696 B telemetry text kept. The first bench try, with
aiscap's 3,600-count ring, returned -8 (scratch too small) because the telemetry text takes the
arena's end. It shares aiscap's polled capture (`c/k6/mod/aispoll.h`)
and decodes every burst 11 dB or more over the floor, alternating channels B and A each minute:

- A message 4 or 11 with a slot number whose second agrees with the message's second is a fine
  fix: the start flag was at `slot x 60/2250 s + 32 bits` into the stated minute.
- A message 4 or 11 without a slot is a coarse fix, to the second.
- Once the clock is within a few seconds, any slot number from a station on UTC (sync state 0
  or 1; messages 1, 2, 4, 11, 18) is a fine fix: the nearest minute, plus the slot.
- The flag's time on the module's own clock is the capture's end (api->ticks, 100 us, read every
  ~50 ms while listening) less the counts after the flag at the TIM17-measured count rate, less
  the receiver's delay (60 us, the model's 0.6 bit).
- A fine fix more than 100 ms from the clock within 10 minutes of the last one is refused (a
  station off UTC). Two fine fixes a minute apart give the K6's clock rate in ppm.

On the emulator (`tests/test_k6_ais.py`), a base station report and a ship's position 38 slots
later each land within 0.2 ms of where the model sent them, and the second fix agrees with the
first to 0.2 ms. The `clk` line is aprsclk's:

```
a 4 3160001 B r=20 s=1575 t=9032          type, MMSI, channel, dB, slot, flag on the module clock (100 us)
clk 03:15:42.086 w=2 v=1 d=09/11 j=0      UTC now, interval ms, fine fixes, day/month, jump (100 us)
ais clk none                              2 minutes with no frame: no AIS here
```

## Run 3, 2026-11-09 04:10-04:25Z (aisclk 579f7b3, af064d6 vlrh, big antenna, receive only)

The log is `tests/data/k6/air/aisclk-20261109T0358Z.log.gz`. RF POWER read 0 before and after.
The first try (a46a5be) returned -8. With the upload's 696 B telemetry text kept at the arena's
end, the scratch was 5,296 B, and the 3,600-count ring didn't fit. 579f7b3 uses 2,960 counts.

- 52 frames in 15 minutes, all from 003669705: message 4 at :00, :20 and :40 and message 20 half
  a second after each, on both channels, at 12-15 dB over the floor. ret = 7 fine fixes.
- The first message 4 set the clock to the second (w=1000). The first with a slot number (1534,
  04:12:40) made it fine, a jump of 356 ms, and six more slot fixes followed.
- **The bursts sit on the slot grid to 0.1 ms.** Put every frame's flag time through one straight
  line fitted to the slot fixes, and its phase within the 26.67 ms slot moves smoothly. Frames
  0.5 s apart (message 4 then 20) agree to 0.1-0.2 ms. So the receiver's timing is good to about
  0.1 ms, and what moves is the K6's clock.
- **The K6's clock runs 134 ppm fast and wanders.** Against a straight line, the slot fixes scatter
  by 4 ms rms (up to 7 ms) over 12 minutes, because its rate changes by up to ±100 ppm over a few
  minutes. Fixes 2 minutes apart jumped -6 to -24 ms with no rate correction. (Aura's 100 us tick
  runs from the MCU's clock, not the RF chip's 26 MHz crystal, and the calibration record's
  -53 ppm is the RF chip's.)
- Replayed on run 3's 52 flags, snapping to the slot grid on every burst (within 10 ms of the
  prediction), with the rate tracked from fix to fix, predicts each slot fix to within 0.6-2.7 ms
  instead of 6-24. That's in aisclk now: SNAP_S, and `p=` is the tracked rate.
- Against the serial log's NTP stamps, the clk lines read 17-148 ms early. That's the line's
  trip through the serial logger (the stamps come in batches), so NTP over the cable can't check
  the clock below ~0.1 s. The slot grid can.

## Run 4, 2026-11-09 04:37-04:52Z (aisclk 05d7555 with snapping, then the IF sweep)

The logs are `tests/data/k6/air/aisclk2-20261109T0435Z.log.gz` and `sweep-20261109T0435Z.log.gz`.
RF POWER read 0 before each upload.

- **aisclk: ret = 54 fine fixes from 55 frames in 15 minutes.** The first message 4 set the second;
  the next, with a slot number, made it fine, and from then on every burst snapped the clock to
  the slot grid. Each kind of line kept its millisecond to ±1 ms for 14 minutes: channel B's
  message 4 at xx:x0.992-.993, channel A's message 4 at x0.939-.940 and message 20 at x1.686-.687.
  The jump against the prediction (`j=`) was ±1-3 ticks (100 us) between bursts 0.75 s apart and
  up to ±4.7 ms after a 40 s gap on the other channel. The tracked rate (`p=`) went 15-179 ppm.
- **The sweep** (`aiscap_sweep`, 05d7555): 40 s on each of 162.025 MHz ±0, 5, 10, 15, 20, 30, 40
  and 60 kHz, the trigger on the whole band of the counts. Its strongest bursts each turn fall on
  the base station's 20 s cycle, so they are the station seen off tune. On the plus side (clean:
  nothing above 162.025 here) they read 10 dB over the floor at 0 and +5 kHz, 11-12 at +10 to +30,
  10 at +40 and 7 at +60. The minus side heard channel A's bursts instead (161.975 is 50 kHz
  below), so it says nothing about B. The turn's 2 captures caught the first bursts over 2 dB,
  mostly the 4 s local junk, but the few with signal put it at |16.9 kHz - offset| (3.6 kHz at
  +20, 13.5 at +30, 23.4 at +40), as a real signal tuned off would land.

## Run 5, 2026-11-09 05:07-05:22Z (the IF's width: NOAA carriers, then the sweep rerun)

Both receive only, RF POWER 0 before and after.

- **The RSSI path is ±5 kHz.** The explorer's scan module (`mod-scan.bin`, 05d7555) swept
  162.350-162.650 and 161.900-162.150 MHz in 2.5 kHz steps for 3 minutes, reading the chip's RSSI.
  Three NOAA weather carriers (162.425, .475, .525 MHz, -91 to -97 dBm on a -114 floor) read 10-20
  dB over the floor out to ±5 kHz and were back in the floor by ±7.5-10 kHz. That's the channel
  filter the RSSI reading sits behind. (Files on hamlet: `~/sotto-work/runs/20261109T0507Z-ifskirt/`.)
- **The GP0 tap is much wider** (`aiscap_sweep` 9e28202, log `tests/data/k6/air/sweep2-20261109T0514Z.log.gz`).
  Tuned 0-200 kHz above the base station, the strongest bursts kept its 20.00 s cadence out to
  +60 kHz: r = 10-11 dB over the floor at +10, 12 at +20, 11 at +30, 9-10 at +40, 8 at +50, 7 at
  +60, then the floor from +80. In each capture the burst sits at |16.9 kHz - offset| (6.7-7.5 kHz
  at +10, 11.5-12.7 at +30, 21-21.5 at +40, 31 at +50).
- **The host decodes the station's message 4 off tune:** from 7 of the 10 off-tune captures, at
  +10, +30, +40 and +50 kHz, by moving the receiver's IF to where the burst lands (a burst landing
  below zero comes through mirrored, which NRZI doesn't mind). So the tap is taken before the
  channel filter, and the counts carry 0-42 kHz of it at 256 reads a count.

## Run 6, 2026-11-09 05:44-06:18Z (both channels at once; aisclk into the core clock)

On cf9516e vlrh (main with #167: API level 8), receive only, RF POWER 0 before and after each
upload. Logs: `tests/data/k6/air/dual-20261109T0543Z.log.gz`, `aisclk3-20261109T0543Z.log.gz`,
`clkget-20261109T0543Z.log.gz`.

- **Both channels from one tuning** (`aiscap_dual`, 162.009 MHz, 10 min): 32 bursts dumped, every
  20.00 s on the base station's cadence. The host (`--tune 162009000`) decodes 26 frames on
  channel A at 17.1 kHz in the counts and 15 on channel B at 32.9 kHz, often both in one capture,
  B 53.3 ms after A. Two strong bursts off the cadence (r = 19-20) decoded on neither.
- **aisclk into the core clock** (714a454, 15 min): 35 frames, 25 fine fixes, each handed to the
  core with `clock_set`. clkget then read 06:11:55.074 w=150 (the serial stamp 06:11:55.043) and,
  5.6 minutes later, 06:17:32.238 w=234 (a batched stamp, late by up to one read): both inside
  their bounds, and the bound grew 84 ms in 337 s, the core's 250 ppm. The first on-air proof of
  the level 8 clock.
- **A gap stranded the clock.** About 6 minutes in, the floor rose (244-335 to 477-582, cause
  unknown, nothing at hamlet transmitted) and frames fell from 5 a minute to 0-2. The next frame
  came 100 s after the last fine fix, past SNAP_S, and none of the 10 frames after carried a slot
  number, so none re-anchored the clock. The station names its message 4 slot only every other
  time, but the slots never change (B: 34/784/1534 at :00/:20/:40, A: 32/782/1532). aisclk now
  keeps each channel's slot less its second x 37.5 and fills in the slot of a message 4 that
  leaves it out (+36 B of code, 2 B spare in the arena with telemetry).

## One clock from AIS and APRS

The two modules can't share one 11,776 B arena (5.8 KB and aprsclk's), so they run one after the
other, and both feed the same clock:

1. Run aisclk. Wherever a base station is in range, it has a fine fix within 20 s.
2. If it decodes no AIS frame in GIVEUP_S (2 minutes, both channels), it says `ais clk none` and
   returns 0. The caller then runs aprsclk, which returns its votes (2 or more means set).
3. Both say the same `clk HH:MM:SS.mmm w=<ms> v=<n> [d=DD[/MM]]` line. aprsclk prints
   `--:--:--` before it has two votes, so a parser skips that. aisclk adds `j=` and `p=`.
4. With the core clock (API level 8, built 2026-11-09: docs/k6-aprs-clock.md), both call
   `clock_set(utc_ms, date, w_ms)`. The core keeps the fix with the smaller w, and grows it by
   250 ppm as it ages (the MCU's RC clock, not the calibrated TCXO). w is an ordering and an
   upper bound on the error, not ±w/2: aprsclk's estimate isn't the centre of its interval. AIS's
   w is 2 ms, and APRS's is 3,000 ms or more, so AIS wins wherever both are heard.

The core clock is in (PR #167). On a level 8 image, aisclk hands every clk line to it: the UTC
of that moment, the date with the full year from message 4, and w = 2 ms once slot-locked, or
1,000 ms from a message 4 second alone. Run 6 showed it on air: the core still read UTC inside
its bound 15 minutes later. A hamlet script still runs the AIS-then-APRS sequence over the cable
until a firmware sequencer does.

## Where AIS works

AIS needs ship traffic or a shore base station within about 20-40 km (VHF line of sight, more
with height): the coasts, the Gulf, the Great Lakes and the St. Lawrence, and the big barge
rivers (Mississippi, Ohio, Columbia). It's unlikely in the Plains, the Rockies, the Great Basin,
the desert Southwest, and inland Canada and Alaska, which is where APRS has to cover. Shore base
stations are the best source (message 4 every 10 s on each channel, with a slot number). Lynden
hears US base station 003669705 at 12 dB or more over the floor on the big antenna.

## RDS (asked 2026-11-09)

Jeff asked whether FM broadcast's RDS clock (group 4A, once a minute on the 57 kHz subcarrier)
could serve. It needs the chip to pass the broadcast swing (±75 kHz, with the 19 kHz pilot and
RDS as sidebands at ±19, 38 and 57 kHz), and the K6 has no broadcast FM chip of its own.

Run 3 took 12 IF captures on 104.3 MHz (`aiscap_fm`, the pager's AGC on; the 12 captures all went
to the first station, so 99.3 got none). FM-demodulated and averaged on the host, they show no 19
kHz pilot line (3 dB over its neighbours, where controls at 15 and 21 kHz show 0.5-2.4), and
nothing at 38 kHz or at 57 kHz's alias. Their IF spectrum is flat from 0 to 41 kHz, with twice
AIS's spread of counts, which looks like the strong station overloading the 1-bit tap.

Runs 4 and 5 measured the IF (above): the chip's RSSI reading sits behind a ±5 kHz channel
filter, but the GP0 tap the capture modules read is taken before it and passes stations up to
50-60 kHz below the tuned frequency (the other side is unmeasured: channel A got in the way). That still rules RDS out, for a different reason. The tap is
one real bit stream at a 16.9 kHz low IF, so anything more than 16.9 kHz below the station's
centre folds back on top of what's above it. A broadcast station swings ±75 kHz, so its FM can't
be undone from the counts, at any count rate. Tuning far enough off to put the whole station on
one side of zero would need a tap passing ~150 kHz, and it's in the floor by 80. So no RDS on the
K6, and the 104.3 MHz captures' flat spectrum is the station folded over itself.

## ACARS (asked 2026-11-09)

Jeff suggested ACARS (AM, MSK 2400 bit/s, 131.550 and 130.025 MHz). The IF tap can see an AM
channel, and its 1200/2400 Hz tones are close to the APRS tones aprsclk decodes. But ACARS has no
clock field: the times are in the message text, mostly to the minute and stamped at the event, so
a clock would vote across aircraft, behind APRS. Run 3's 2-minute listen (`aiscap_acars`, AGC on,
04:08Z, a quiet hour) heard no burst 6 dB over the floor on either channel. Airband sits below the
K6's 136-174 MHz filter. A longer listen at a busy hour, with the gain at maximum, would settle it.

## Bench (receive only)

1. A voice image (vlrh) on the K6, the big antenna on it, cable in, at VOICE MSG.
2. `tools/k6_upload.py mod-aiscap.bin --api-level 2 --run-s 620`, keeping the serial log.
3. `python3 -m sim.k6if.ais cap LOG` decodes every capture. The `f` lines give each turn's rate
   and floor, the `m` lines its bursts.
4. The clock: `tools/k6_upload.py mod-aisclk.bin --api-level 2 --run-s 920`. Compare its `clk`
   lines with the log's NTP stamps (the line comes ~0.1 s after the UTC it states).
5. RDS: `tools/k6_upload.py mod-aiscap_fm.bin --api-level 2 --run-s 40`, then the captures'
   spectrum on the host.

## Next

- Both AIS channels at once works on the host (run 6). For the radio: aisrx.h with the IF as a
  parameter, both channels decoded from each capture, so the clock and the fuller receiver stop
  switching channels every minute.
- A fuller AIS receiver (Jeff, 03:35Z): ships' names, positions and courses on the screen. To be
  scoped once the clock works.
- ACARS (Jeff, 03:54Z): a coarse clock after APRS if the K6 hears it, and then a fuller ACARS
  receiver (flights, positions, messages) too (Jeff, 03:55Z).
- The core keeping UTC between modules (API level 8) serves both clocks.
