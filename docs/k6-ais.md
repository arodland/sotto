# AIS on the K6: ship traffic as a clock

*2026-11-09.* The APRS clock works (docs/k6-aprs-clock.md), but no RF station near Lynden sends
APRS timestamps. Jeff asked which other signals in the K6's range (18 to 630 MHz) carry time, and
chose AIS from the list. Ships and shore stations on 161.975 and 162.025 MHz send time all day,
and the Salish Sea is 15 to 25 km from the bench.

**Status: model and capture module built, emulator-tested, not yet on air.** The host receiver
(`sim/k6if/ais.py`) decodes AIS from modelled K6 IF counts at about 55 dB-Hz and up. The capture
module (`c/k6/mod/aiscap.c`) records each burst whole on the emulated M0. The open question is
the bench: are ships and base stations loud enough at the K6?

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

Things tried that didn't help on the model: a 4-6 kHz FIR after the CIC (the chip's filter and
GMSK's own spectrum leave nothing to gain), decimating by 2 (too much of the sigma-delta's noise)
or by 4. Two that might: flipping the one or two least certain bits when the CRC fails (about 1
dB in other AIS receivers), and a coherent GMSK receiver on the complex baseband.

The start flag's time comes out about 0.6 bit (60 µs) late on the model, from the chip filter's
and the CIC's delay. That's a constant, to be calibrated once.

## aiscap: recording what the K6 hears

`c/k6/mod/aiscap.c` (level 2, receive only) needs a voice image's 11,776-byte arena: its 1 KB DMA
ring and 3,456-count ring (40 ms) live in the scratch, so the stack stays under 600 B. It
freezes the gain at REG_13 = 0x03FF (what DMR needed), alternates channel B and A each minute,
and reads the RSSI every 64 counts while the count ring runs. When the RSSI rises 8 dB over the
channel's floor, it records 28 ms more and stops, so each capture holds 12 ms from before the
RSSI saw the carrier and the whole burst after. The dumps are in gp0cap's format, so
`sim/k6if/dmr.py`'s parser reads them. A burst past the 60th is only counted (`b` lines).

On the emulated M0, two bursts 0.9 s apart are both recorded whole and decoded by the host. A dump
takes about 0.5 s on the cable, so bursts in that time are missed.

## Bench (receive only)

1. A voice image (vlrh) on the K6, the big antenna on it, cable in, at VOICE MSG.
2. `tools/k6_upload.py mod-aiscap.bin --api-level 2 --run-s 620`, keeping the serial log.
3. `python3 -m sim.k6if.ais cap LOG` decodes every capture. The `m` lines give each channel's
   floor and how many bursts went over it.

What would change the plan:

- No `c` lines at all: nothing reaches floor + 8 dB. Try a better spot for the antenna, or
  `SQ_DB` lower.
- Captures but no frames: look at their RSSI. Below about -75 dBm the model says too weak.
  Above that, the receiver needs work on real data.
- Frames: the clock module comes next (below).

## Next

- **aisclk**: the receiver on the M0 (CORDIC and the CIC as aprsclk, the HDLC already written
  there), live on one channel. It would set the clock from message 4 (date and second) and refine
  it from slot-number bursts of stations on UTC. Several stations' slot times should agree to a
  few tenths of a millisecond.
- The core keeping UTC between modules (API level 8, proposed in docs/k6-aprs-clock.md) serves
  both clocks.
