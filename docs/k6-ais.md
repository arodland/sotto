# AIS on the K6: ship traffic as a clock

*2026-11-09.* The APRS clock works (docs/k6-aprs-clock.md), but no RF station near Lynden sends
APRS timestamps. Jeff asked which other signals in the K6's range (18 to 630 MHz) carry time, and
chose AIS from the list. Ships and shore stations on 161.975 and 162.025 MHz send time all day,
and the Salish Sea is 15 to 25 km from the bench.

**Status: the K6 hears AIS but hasn't decoded it yet.** Bench run 1 (2026-11-09 02:48Z, below)
heard 739 bursts in 10 minutes on both channels. The best came in about 5 dB under what the
receiver needs, and the limit was the K6's own 1-bit IF tap as the DMA samples it, not the band's
noise. aiscap now polls the tap at 22.9 MS/s instead, which moves the threshold about 6 dB on the
model. Run 2 tries that.

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

## Next

- **aisclk**: the receiver on the M0 (CORDIC and the CIC as aprsclk, the HDLC already written
  there), live on one channel. It would set the clock from message 4 (date and second) and refine
  it from slot-number bursts of stations on UTC. Several stations' slot times should agree to a
  few tenths of a millisecond.
- The core keeping UTC between modules (API level 8, proposed in docs/k6-aprs-clock.md) serves
  both clocks.
