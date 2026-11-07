# K6 receive coverage and sensitivity (rxmap)

How far does the Baofeng K6 really receive, and how well? The FD6818 (BK4819 numbering) is said
to tune about 18-630 and 840-1300 MHz, but the K6's front end is filtered for about 136-174 and
400-520 MHz. Neither number has been checked on this radio. The `rxmap` RAM module measures it,
in two modes:

- **map** (receive only). It sweeps RSSI across a segment, bin by bin, and records each bin's
  noise floor and its strongest signal. Off-air signals are the test signals: FM broadcast,
  VOR, NOAA, TV, pagers, cellular. Where the filters cut, their peaks sink into the floor. The
  K6 transmits nothing except the usual upload report on 440.850.
- **cal** (the IC-7100 transmits). The IC-7100 keys a bare carrier on a list of 2 m and 70 cm
  points, then steps its power down at 440.850, and the K6 measures each carrier. This shows
  the in-band response and how RSSI tracks a known change in level. The REG_63 noise
  indicator (0 at full quieting) shows how much FM quieting each carrier gives.

Files: `c/k6/mod/rxmap.c` (the module, MIT, 2.6 KB at `-Oz`, API level 2), `tools/k6_rxmap.py`
(plan, run, decode, plot), `tests/test_k6_rxmap.py` (the module on an emulated M0 against a
modelled receiver).

## How map reads a bin

For each bin (2 MHz in the full sweep), the module:

1. tunes every `step` (25 kHz with the wide filter, so it can't step over an FM signal) and
   takes one quick RSSI read 3 ms after the tune;
2. re-reads the loudest and the quietest of those channels properly: tune, wait 30 ms, then the
   mean of 4 reads 1 ms apart. The loudest gives the bin's peak and the quietest its floor.

This rests on the explorer's Phase 0 measurement (2026-11-06, the K6 signal explorer doc): REG_67
restarts at -173 dBm on every tune and ramps to the true level in about 12 ms wide, whatever the
step. At 3 ms a strong carrier already reads -82 against -145 for an empty channel. So quick
reads taken at the same moment rank a bin's channels, and the two full reads give true levels.
The settled empty channel is still creeping up at 20 ms, so the full read waits 30 ms.

A 320 MHz segment (160 bins of 80 steps) takes about 56 s on the K6. The whole 18-1298 MHz
range is four segments, A to D. The 630-840 MHz "gap" is scanned too: what the synthesizer
does there is part of the question.

## What comes back

The module's lines go on the serial report (`K6 MOD say ...`). With a reply-wanted upload
(`k6_upload.py`, magic 0xAE), the K6 also sends the newest 12 x 57 bytes back over the air as
telemetry after the run, on 440.850. So the module prints its detail first and a compact
summary last, and the summary is what survives in those 684 bytes:

```
b 162.000 -131 -61 @162.550 z0000     per bin, serial only: floor, peak, peak's frequency, REG_63
T 162.550:-61 98.100:-35 ...          the 12 bins most over their floor, strongest first
F0 <50 chars> ... P0 <50 chars> ...   floor and peak rows, one char a bin: '0' + (dBm + 160) / 2
M lo=18.000 b=2000 n=160 s=25000 q=3000 sig=12 ms=55120
```

`tools/k6_rxmap.py decode` reads either form. It uses the serial detail where it has it (1 dB,
with the peak's frequency) and the rows otherwise (2 dB steps). It prints the coverage per
region (median floor and signals 10 dB or more over it, with likely services), lists the
signals, and draws an SVG with `--svg`. Peaks within 30 kHz of a 26 MHz harmonic are starred:
the K6's own crystal.

cal prints the floors (`C 440.850:-131 ...`), then one line per carrier as it drops
(`c 440.850 -61 -60 fl-131 n8 z0`: mean and max dBm, floor, readings, mean REG_63), then
`K ev=N ms=...`.

## Running it (hamlet)

The bench steps, with the exact keys, are in the project files at
`k6-rxmap/bench-keys.md`. In short:

```
python3 tools/k6_rxmap.py plan
python3 tools/k6_rxmap.py map A B C D --bin mod-rxmap.bin --key-file ~/.config/sotto/k6-mod.key
python3 tools/k6_rxmap.py map fm air vhf uhf gap --bin ...     # finer looks where A-D say so
python3 tools/k6_rxmap.py cal --bin ... --key-file ...         # IC-7100 transmits: see below
python3 tools/k6_rxmap.py decode rxmap-*.log --svg rxmap.svg
```

hamlet has no clang, so `--bin` is CI's `mod-rxmap.bin` (in the *K6 pager firmware* artifact).
The tool finds the 100-byte parameter block after `RXMAP v1` in the binary and patches it
before `modem.k6 mod` signs it. Any segment can be given as `LO-HI[/BIN_kHz[/STEP_kHz]]` in MHz.

**cal transmits.** It keys only US amateur 2 m (144.010-147.990) and 70 cm. Lynden is north of
Line A, where 420-430 MHz is closed to amateurs, so 70 cm is 430.010-449.990 unless
`--south-of-line-a` is given. Each point gets 2.5 s of carrier and the station's CW ID at
25 wpm, in `PKTFM 12000` (PR #81: 0 selects the 6 kHz filter at half deviation). The power
steps are fractions of the radio's own RF POWER setting, which is restored at the end. A dummy
load on the IC-7100 keeps the K6's signal in its linear range: Phase 0's bench carrier read
-20 dBm, far above anything a sensitivity number needs. The default points are 144.050,
145.550, 146.550, 147.450, 147.950, 430.100, 433.000, 440.850, 445.950 and 449.900 MHz.
`--points` changes them, so check them against the local band plan first.

## What it can't tell

- The noise floor is the chip's own, as REG_67 reports it. It doesn't drop where the filters
  cut, because the K6's own noise dominates there. That's why map uses off-air signals.
- The coverage measured is the radio plus its stock antenna, which is a 2 m / 70 cm whip.
- RSSI is uncalibrated outside the bands Aura calibrates, and may not be linear. cal's power
  steps check its slope at 440.850.
- True sensitivity (12 dB SINAD or a packet error rate) needs a calibrated signal source.
  The cal levels against REG_63 come closest.

## Status

2026-11-06: coded and emulator-tested (PR to come with this file). Not on air yet: the K6 is
hung until a battery pull, about 01:00 UTC 2026-11-07.
