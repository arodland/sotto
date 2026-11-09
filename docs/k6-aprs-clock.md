# K6 APRS clock: UTC from packets heard on 144.390

*2026-11-08.* Jeff's idea: the K6 has no clock (the WSPR beacon uses the bench's CW ID as its sync
mark, `docs/k6-ft8-wspr.md`), but APRS traffic carries the time. `c/k6/mod/aprsclk.c` is a
receive-only RAM module that listens on 144.390 MHz, decodes APRS packets on the MCU, reads
their timestamps and votes them into a UTC clock. Nothing is transmitted (an upload sent with
`--arq` still gets its usual report on 440.850 after the module returns).

**Why not Aura's APRS code.** Aura (commit `edf3b98`, the one the pager patch is based on) can
only *send* APRS: `app/ax25/` builds a position beacon (address field, CRC, NRZI) and
`drivers/fd6818.rs` keys it out as Bell 202 by switching the chip's tone oscillator between
1200 and 2200 Hz on a TIM6 bit clock. There is no demodulator, no HDLC deframer and no parser,
and the chip's own FSK modem can't receive AX.25 (it wants its sync words and fixed lengths).
The receiver had to be new either way, so it is MIT C on the IF tap the FT8 and softmodem
modules already use. If the K6 ever sends APRS, Aura's tone switching is the reference for that
module.

## What it does

```
GP0 code 13 on PB1 -> DMA ch1 ring (1 KB, ~5.5 MS/s)                       (gp0rx.h's front end)
 -> counts of 64 samples (~86 kHz) -> mixed down by the nominal IF (64-entry sine)
 -> 2nd-order CIC, decimate by 6 (~14.3 kHz complex)
 -> CORDIC phase (11 steps); the phase step is the FM discriminator (no division)
 -> DC removed; sliding correlators over one bit at 1200 and 2200 Hz; mark > space
 -> DPLL bit clock (nudged towards each transition, firmer inside a frame), NRZI
 -> HDLC: flags, bit stuffing, CRC-16/X.25 (good residue 0xF0B8)
 -> the APRS information field's timestamp -> one interval per station -> Marzullo's vote
```

**Timestamps it reads.** `/` and `@` positions, `;` objects and `>` status reports with
`DDHHMMz` (zulu, one-minute resolution) or `HHMMSSh` (zulu, one second); raw NMEA
`$GPRMC`/`$GNRMC` with status `A` (`hhmmss[.ss]` and the date `ddmmyy`); positionless weather
reports `_MMDDHHMM` (zulu by the spec, one minute, with the month). It ignores `DDHHMM/`
(local time, zone unknown), Mic-E (no time), `}` third-party traffic (internet delays) and
everything else.

Positionless weather was added after the 2026-11-09 survey of the worldwide feed: 0.6% of the
packets heard on the air are `_MMDDHHMM`, but only 18 of 102 were within 10 minutes of UTC; most
sent local time or a stale stamp. Stations in one time zone sending local time would agree with
each other, so weather stamps count in the vote only beside another kind: agreement among weather
stamps alone is one vote, never a clock (`test_weather_stamps_alone_set_nothing`).

**The vote.** A stamp says the true UTC at the frame's first bit (the frame's end, less its bits
at 1200 baud) was between the stamp and the stamp plus its resolution plus a delay: 3 s heard
direct, 8 s through a digipeater (any H bit in the path). In offsets (UTC ms of day less the
module's ms, taken round the day so midnight is no edge) each station keeps one interval, the
newest; a digipeated copy of the same stamp is not a second vote. Marzullo's algorithm (NTP's)
finds the offset the most stations agree on, the narrowest region among ties, so a station with
a wrong clock is outvoted. With two or more agreeing the clock is set: the estimate is the
region's low edge plus half its width, at most 0.5 s, because a stamp can't be later than the
send and GPS trackers key within a second of their fix. With minute stamps only (a region wider
than 10 s) it is the region's middle instead, within about 30 s. A seconds stamp in the vote gives the
second ("(sec)" on the screen); `DDHHMMz` alone gives the minute. The date comes from the newest
agreeing stamp that has one (`$GPRMC` gives day/month/year, `DDHHMMz` the day).

**On the radio.** Level 2 to run (receive, GP0, TIM17, DMA channel 1, all put back as gp0rx.h
does); at level 6 it draws its screen (UTC, stations voting, frames good/bad/stamped, the
date) once a second, never while a frame or its flags are coming (a flush takes 20-40 ms and
the ring holds 0.2 ms); at level 7 it blinks the green LED on each good frame. Any key ends it,
else 15 minutes (`RUN_MS`). It tunes 144.390 with the wide filter and leaves the chip's AFC on,
which pulls a sender's offset in (unlike FT8's narrow tones, AFSK on FM doesn't mind). Lines:

```
aprs start hz=144390000 ks=5490 n=12
f VE7ABC-9 n=47 h=0 ts=17:52:00h               (a good frame: source, bytes, hops, stamp and kind)
clk --:--:-- w=4000 v=1                        (the clock after each stamp: UTC, region ms, votes)
f VE7WX n=67 h=1 ts=17:51:00z
clk 17:52:03.060 w=4000 v=2 d=08
f N0CALL-7 n=81 h=0 ts=17:52:04.220r
clk 17:52:05.274 w=3010 v=3 d=08/11
aprs end ok=3 bad=0 ts=3 v=3 utc=17:52:06.562 lost=0
```

It returns the votes behind the clock (0: not set), or -2 (a key before any frame), -3 (old
image), -4 (PB1 not an input), -5 (REG_33 refused), -7 (TIM17 or DMA channel 1 in use), -8 (the
scratch too small).

## Size: which images

5,046 bytes of code (clang -Oz) and about 2.3 KB of state (the DMA ring 1 KB, a frame 332 B, the
correlators 256 B, twelve stations 336 B), 288 B of stack. That needs the shared arena of the SPI
images (`aura-pager-only`, `-lr`, `-vlr`, `-vlrh`: 11,776 B since PR #146). It does **not** fit
the EXPLORE image or the lean images (5,376 B block): the radio needs a voice image, and the
module can move into the SPI module library unchanged once that exists.

## Tested (emulator and host, `tests/test_k6_aprsclk.py`)

Three stations through the study's model of the K6's receive path (sim/k6if: FM, the FD6818's
filter and AGC, the GP0 sigma-delta at 5.49 MS/s): a tracker's `HHMMSSh` stamped 0.4 s before
it keys, a weather station's `DDHHMMz` through a digipeater, and a `$GPRMC` 0.7 s old.

- **Emulated M0** (`tests/k6_live_emu.py`, CPI 1.5 calibrated on gp0dma): all three decoded, no
  bad frames, no counts lost, clock 0.19 s behind the truth (the stamps' own lag), v=3, screen
  and LED as above. It still decodes everything at CPI 4 and 5 (the CPU cost x 3.3), fails at 6:
  the receiver takes roughly a quarter to a third of the CPU.
- **Host build of the same C** (`c/k6/aprsclk_host.c`), 3 seeds each: clean with the space tone
  +6 dB over the mark (a sender's pre-emphasis) or -4 dB, deviation 2 to 4.5 kHz, carrier ±800 Hz
  off. At ±1500 Hz with 3 kHz deviation it loses some frames (peaks past 4.5 kHz); on the radio
  the AFC pulls the carrier in. Noise: 17 of 18 frames at C/N0 52 dB-Hz, 4 at 50, none at 48, about
  where a float discriminator on the same model breaks (13, 9, 0): it's the FM threshold. 52 dB-Hz is
  roughly -116 dBm at the antenna, so local digipeaters and igates, not distant mobiles.
- A peak tracker per tone (to undo pre-emphasis twist) made it worse (lost frames at +6 dB): left
  out. Parser cases: one station isn't enough; a digipeated copy is one vote; three agreeing GPS
  stations outvote one 7 minutes slow; local-time, impossible, no-fix, untimed and third-party
  packets give nothing; objects and status reports count; stamps either side of midnight agree.

## Making it the radio's clock

A module's clock dies with the module: `ms()` counts from its own start. Module API level 8
(Jeff's OK 2026-11-09 04:49Z) gives the radio one UTC clock that lives in the core from one
module run to the next, until a reboot, fed by whichever module hears the time first (AIS
first, APRS if no AIS: Jeff 03:43Z):

    int clock_set(uint32_t utc_ms, uint32_t date, uint32_t w_ms);   /* 1 taken, 0 kept, -1 refused */
    int clock_get(uint32_t *utc_ms, uint32_t *date, uint32_t *w_ms); /* 0, or -1: no clock */

- `utc_ms`: UTC milliseconds of day at the moment of the call (0 to 86,399,999).
- `date`: `day | month << 8 | year << 16`, the year in full (2026), 0 for any part not known
  (`KM_DATE(d, m, y)` in `k6_mod.h`). APRS gives a day from `DDHHMMz`, the whole date from
  `$GPRMC`; AIS message 4 gives the whole date.
- `w_ms`: a bound. UTC is within `w_ms` of `utc_ms`. aisclk's slot fixes are about 2 ms, a
  coarse AIS fix 1,000; aprsclk passes the far end of its Marzullo interval from its estimate
  (about 1.5 s with second stamps heard direct, about 30 s with minute stamps only).

The core runs the fix on from uptime that never wraps (`hal::txdog::ticks`: TIM14's count and
its wrap interrupt, in every image that has the `txdog` table) and widens the bound by 1 ms
every 4 s (250 ppm). The MCU runs on its own RC oscillator, not the radio's TCXO: AIS run 3
measured it 134 ppm fast, wandering ±100, so the calibration record's -53 ppm (the RF crystal)
doesn't apply and the bound is set wide enough for both. A new fix replaces the clock only if
its bound is no wider than the clock's bound at that moment, so a 2 ms AIS fix outranks APRS for
the next 6 hours or so (until 2 ms + 250 ppm passes APRS's 1.5 s), and date parts a fix lacks come
from the fix before. Past midnight the date steps on (month lengths, leap years by 4). After
59.6 hours without a fix the clock is dropped, and `clock_get` says -1. The lean images
(codebook install, table images) have no clock: both calls return -1.

`aprsclk` calls `clock_set` with each `clk` line that has two votes or more (on level 8 images;
on older images it runs as before). `clkget` (level 8) says the core's clock, `core clk
04:52:10.123 w=14 d=09/11/2026` or `core clk none`, and shows it on screen: run it minutes after
aisclk or aprsclk to see the clock was kept. `test_module_sets_the_radios_clock` and
`test_clkget_says_the_core_clock` check both against the emulator's model of the core
(`tests/k6_live_emu.py`).

Cost: about 570 B of flash and 20 B of RAM in each image with the clock (local build of the
pager-only and hiss images). CI had the hiss image (vlrh) at 1,068 B free before it.

**On the radio** (2026-11-09, image cf9516e vlrh). Right after the flash, `clkget` said `core clk
none`, as it should. The 70 cm `aprsclk_440` loopback decoded nothing that time (rssi -130, the
IC-7100's stub at power 0 into the K6's big 2 m antenna), so the first fix came from AIS: aisclk
(714a454) handed the core 25 fine fixes between 05:56:21 and 06:02:02Z. Then clkget read:

| clkget line | hamlet's NTP stamp on the line | bound |
|---|---|---|
| 06:11:55.074, d=09/11/2026 | 06:11:55.043 (31 ms) | w=150 |
| 06:17:32.238 | 06:17:32.387 (stamp late by up to one serial read) | w=234 |

Both are inside their bounds. The bound grew 84 ms in 337 s, the 250 ppm it is set to, and the
clock carried across module runs ten and fifteen minutes after the last fix. Log:
`tests/data/k6/air/clkget-20261109T0543Z.log.gz` (on the AIS thread's branch).

Next uses: the pager screen can show UTC, and the WSPR and FT8 modules can start on their slots
from `clock_get` instead of the bench's CW ID.

## Bench

Receive only on 144.390: upload `mod-aprsclk.bin` to a voice image (any SPI image with the
11,776 B arena), leave the K6 on 144.390 with traffic, and compare the `clk` lines (or the
screen) with NTP on hamlet.

**Run 1** (2026-11-08 21:45-22:00Z, image 006ee1b, 15 min on 144.390): `ok=0 bad=589 ts=0 v=0
lost=1`, stack free 2,276 B, the report went out on 440.850 as planned. No frame passed its CRC.
The 589 bad frames are flag-to-flag runs, which noise makes too, so they do not show whether any
APRS reached the radio: the IF tap had never been tried at 144 MHz, and nobody had checked that
144.390 is busy at hamlet. The module now prints a line every minute, `m <min> rssi=-<dBm> pre=
ok= bad=`, where `pre` counts preambles (three flags in a row inside a run), which noise rarely
makes, so a quiet channel and a deaf demodulator look different.

**Loopback on 70 cm** (`mod-aprsclk_440.bin`, `c/k6/mod/aprsclk_440.c`: the same module on
440.850 for 5 minutes). The IC-7100 sends three AG7EW stations whose stamps are hamlet's UTC at
each frame's first bit:

    python3 -m modem.aprs wav ~/sotto-work/runs/aprs-loop/loop.wav --now
    sh tools/k6_send.sh ~/sotto-work/runs/aprs-loop/loop.wav

The first prints `start` (the time the stamps assume the audio starts) and each frame's end
offset; the second prints `t0`, and the audio starts about 0.2 s after it (PTT settling). The
upload is the usual `python3 tools/k6_upload.py mod-aprsclk_440.bin --api-level 2 --telemetry 12
--run-s 360 --key-file ~/.config/sotto/k6-mod.key --payload XL`, and a CW ID sent just before
keeps the send from putting one in. With exact stamps the interval's low edge is the truth, and
the estimate adds the half second a real station's stamp leads its keying, so the `clk` line
after AG7EW-3 should read (start + its end offset) + 0.5 s. The stamps are early by
D = t0 + 0.2 s minus start, so against hamlet's clock it reads 0.5 s minus D ahead
(`test_bench_loopback_packets` checks 0.45 to 0.55 s through the modelled radio).

**Run 2, the loopback** (2026-11-08 22:49-22:54Z, image 006ee1b, `mod-aprsclk_440.bin` from CI
run 42530008074, 5,480 B as CI's clang builds it): three bursts 100 s apart, every packet
decoded: `ok=9 bad=154 ts=9 v=3 lost=0 pre=9`, ret 3, stack free 1,828 B, RSSI -112 to -115 dBm
(the IC-7100 at RF power 0, a few metres away). Each `clk` line against the frame's true end on
air (audio start = t0 + 0.2 s): -135 to -251 ms, mean about -0.2 s. The bursts' stamps were early
by D = 0.67-0.73 s, so the prediction was 0.5 s - D = -0.17 to -0.23 s: the receive chain, the
parser and the vote work on the radio as modelled, within a few tens of ms. The `bad` count is
noise between frames (about 40 a minute on a quiet channel), so `pre` is the line to read. The
serial logger sees each line about 3.3 s after its frame, which is why a `clk` line looks 3.4 s
behind the logger's own stamp.

**Run 3** (2026-11-08 23:03-23:18Z, image 61cc53a, `mod-aprsclk.bin` 5,480 B, 15 min on
144.390): `ok=0 bad=592 ts=0 v=0 lost=0 pre=0`, `pre=0` in every minute line, so not one packet
preamble. The minute RSSI sat at -85 to -87 dBm, flat for 15 minutes, against -115 dBm on an idle
440.850 in run 2. Either a steady carrier or a K6 birdie (its own synth or an MCU harmonic) sits
on 144.390 (a carrier would also quiet the discriminator, which fits `pre=0`), or the chip's RSSI
reads about 29 dB higher on 2 m. Since the loopback decodes, the open question is the 2 m channel.

**Run 4** (2026-11-08 23:31-23:47Z, image f70e79e, 15 min on 144.390, with the IC-7100
beside it receive only, Jeff's OK): the K6 decoded Jeff's car tracker at 23:39:13Z, `f AG7EW-7
n=62` (Mic-E, so no stamp: `ok=1 ts=0 v=0 pre=1`). The IC-7100 saw the same packet at S9+45 dB
and nothing else in 12 minutes. Its S-meter sat near S7 on 144.390 with the K6 on 2 m and only
3 dB lower with the K6 back on 440.850, and its audio was flat open-squelch noise, so run 3's
-86 dBm is the band's real noise floor at the bench, not a K6 birdie, and 144.390 is simply quiet
there. The recording decoded only through a 1-pole de-emphasis (the mobile's pre-emphasis off
the IC-7100's discriminator), so `modem.aprs decode` now tries flat, 0.95 and 0.9. The K6 decoded
it without one. To test the clock off-bench, a tracker has to send a stamped format: a position
with HHMMSSh or DDHHMMz, or raw $GPRMC.

**Run 5** (2026-11-09 00:09-00:24Z, image 0fb4d6c, 15 min on 144.390): Jeff forced beacons and
sent a message from his car. `ok=7 bad=670 ts=0 v=0 lost=0 pre=9`: three 62-byte Mic-E
positions and four 49-byte frames (the message and its retries), all AG7EW-7. Two preambles did
not end in a good frame. None carried a time, so there is no `clk`. The minute RSSI (-90 to -92
dBm) is the minute's floor, so packets do not show in it. The receiver is proven on 2 m. What is
left is a station that sends a stamped format.

**Who sends stamps near the bench** (APRS-IS feed, 40 km around 48.95 N 122.48 W, 00:36-00:57Z,
2026-11-09): 13 stations heard on RF (digis, igates, trackers, Jeff's car), none with a usable
stamp (one repeater object carries a fixed `111111z`). The stamped weather stations nearby (CWOP)
reach APRS-IS over the internet only. Jeff's FTM-300D sends Mic-E positions only and has no
timestamp option.

**Run 6, big antenna and a stamped burst on 2 m** (2026-11-09 01:09-01:41Z, image af064d6, the
K6 on Jeff's big antenna, two 15 min listens; the IC-7100 on a stub at RF power 0). The minute
RSSI fell to -97 to -101 dBm (from -86 on the K6's own antenna: local pickup, not a carrier).
With Jeff's OK the IC-7100 sent one 4 s burst on 144.390 at 01:17:19Z, no digi path (the
loopback's three AG7EW packets). The K6 decoded all three, voted 3 stations, took the date
(9/11), and read about +0.12 s against each frame's true end, as predicted (stamps 0.37 s early
against the real audio start). Seven minutes later its end line was within 0.14 s of the logger.
Listen 1 also decoded Jeff's car again. Listen 2 heard nothing in 15 min (`pre=0`). So the clock
works end to end on 2 m. At Jeff's location it needs a stamped source of its own, such as a
fixed station beaconing like the burst, since no local station sends one.

**How common stamps are** (worldwide APRS-IS feed, 15 min, 2026-11-09 01:50-02:05Z, packets an
igate heard on RF only): 17,755 RF packets (about 1,180 a minute); 8% carry a usable stamp
(within 10 min of arrival), 95 a minute from 588 stations. About as many again carry a wrong
one (repeater objects at `111111z`, trackers at `000000h`, digipeater clocks hours off, stale
objects), which is why the vote needs two stations. Usable by kind (packets / stations):
DDHHMMz positions from digis and WX3in1 boxes 515 / 295, HHMMSSh trackers 334 / 99, weather
297 / 162, objects 232 / 53, status 39 / 29, raw $GPRMC 5 / 1. Seconds stamps arrive 2-3 s
after their stamp, mostly internet delay; minute stamps about 50 s. Europe and North America
are alike (about 40 usable a minute each); within 300 km of the bench only 1.2 a minute from
12 stations, mostly minute stamps, two with seconds (41 and 98 km). Positionless weather
(`_MMDDHHMM`, 0.6% of RF packets) carries a date and time the module does not read yet.

This tests the IF tap, demodulator, HDLC, parser and clock on the real radio; if it passes and
144.390 still decodes nothing, the next step is an IC-7100 recording of 144.390 (receive only)
run through `python -m modem.aprs decode REC.wav` to count local stamped packets.
