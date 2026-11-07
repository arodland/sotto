# K6 fast scan (explorer Phase 1)

The first working piece of the K6 signal explorer (design: the "K6 signal explorer" Claude Doc,
section "Fast scanning: a two-stage sweep"). A level 2 RAM module, `c/k6/mod/scan.c`, sweeps up
to four segments in turn, finds carriers, follows each one it finds, and reports every
transmission ("over") with its length and level. The host side is `tools/k6_scan.py`. The K6
only listens: the module never transmits and writes no register (`tune` is how it moves).

It runs on the image already on the radio (API level 2 or later), so nothing needs flashing.

## How it works

Each bin of a segment:

1. **Coarse.** Tune, wait `quick_us` (1.5 ms), read RSSI once.
2. **Noise test.** Each bin keeps its own floor of those quick reads. Phase 0 showed RSSI restarts
   from -173 dBm on every tune and ramps for 12 ms (wide), so a quick read is far below the true
   level, but a carrier still reads about 35 dB over an empty channel read at the same moment.
   So the floor is a quick read too, never the settled level. A read under floor + 6 dB is noise.
   The floor falls halfway to any lower read and rises 1 dB every 8 sweeps while the bin stays
   quiet. The first sweep only learns, with each floor capped 3 dB over the segment's quick
   floor, so a carrier that is up all through the first sweep still shows later.
3. **Confirm.** A bin that passes waits on to `confirm_us` (12 ms) after its tune and reads twice:
   a hit if that is 10 dB over the segment's settled floor.
4. **Refine** (new hits only). Narrow-filter reads every 2.5 kHz over ±(step + 5 kHz), the middle
   of those within 3 dB of the loudest, snapped to the segment's channel raster, then a full read
   there for its level.
5. **Revisit.** Hits go on a hot list of 10 entries, visited between blocks of 32 bins, every
   100 ms. A quick read says up or down. An over starts with a full read (its level) and ends
   after two down reads, with one `H` line. An entry up for longer than `spur_s` (60 s) within
   3 dB is a spur: it gets one `X` line and is never visited again. Bins within 1.5 steps of an
   entry that's up skip the confirm, so the skirt of a strong signal doesn't make extra entries.

## Running it

```
python3 tools/k6_scan.py plan                     # segments, fit, estimated sweep times
python3 tools/k6_scan.py run 2m --bin mod-scan.bin --key-file ~/.config/sotto/k6-mod.key \
    --seconds 120 --out ~/sotto-work/runs/<run-folder>
python3 tools/k6_scan.py run 2mo 70a --seconds 300 ...   # two segments in turn
python3 tools/k6_scan.py decode LOG --segments 2m --svg wf.svg --csv hits.csv
python3 tools/k6_scan.py watch LOG                # live terminal waterfall from a growing log
```

`run` patches the segments into the module's parameter block (`SWEEP v1`), signs it, and sends
it with `tools/k6_upload.py` (ARQ, telemetry 12). When the run ends, the K6 answers on 440.850
with the summary. The waterfall rows only go to the serial port; to keep them, run
`python -m modem.k6_harness listen --k6 PORT --log FILE` by hand (it only reads the port) and
point `watch` or `decode` at the file. hamlet has no clang, so give `--bin` the CI-built
`mod-scan.bin` from the K6 pager firmware artifact.

Segments (`plan` lists them): `2m` 144-148 at 10 kHz, `2mo`/`2mr` the 2 m repeater outputs,
`125` 222-225, `70a`/`70b` 440-445/445-450 at 12.5 kHz, `70w` 440-450 at 25 kHz, `70s`
430-435, `noaa` (an always-on carrier, a spur test). Or any `LO-HI/STEP_kHz` in MHz.

## Lines

| Line | Meaning |
|---|---|
| `F0 -137 q-160: -137 -136 ...` | a segment's settled and quick floors at the start, then its 8 full reads (taken off the round frequencies) |
| `N 146.940 -71 fl-137 z0 b123` | a new hit: centre, level, segment floor, REG 0x63 noise indicator, bin |
| `H 146.940 2030 -71 a61250` | an over ended: centre, ms up, level, when it started (module ms). The first run printed the overs so far (`n3`) instead; `decode` reads both |
| `X 146.940 -61` | a spur |
| `R0 0 0000a3...` | one sweep's row, max-pooled to `cols` (128) columns: `'0'` + dB over the bin's floor |
| `S0 12 702 c14 h1` | segment 0, sweep 12: 702 ms, 14 confirms, 1 hit |
| `T 146.940 n3 t5 -71 d2` | the summary, one line an entry: overs, seconds up, level, spread of the overs' levels in dB (`s` = spur) |
| `Z sw=40 bins=16000 ms=30010 bps=533 hot=3` | last: sweeps, bins, ms, bins a second, candidates |

The `T` and `Z` lines come last so the telemetry (the newest 684 bytes) always holds them.
`decode` also works out what a coarse bin really costs from the `S` lines of sweeps with no
confirms: their time over their bins, less `quick_us`. That is the number the explorer design
still lacks (the first run: 0.92 ms; the emulator charges 0.42 ms, the design assumed 0.25 ms).

## Memory

The module is 3.5 KB of the 4 KB store. The scratch RAM after it holds the state (176 B) and the
hot list (10 × 28 B), which leaves about 150 B to spare with this machine's clang and about 130 B
with CI's (CI's build comes out about 24 B bigger). The stack holds the floors, one byte a bin
and up to 1,024 bins in all, then the waterfall row: bins plus row columns up to 1,184. Phase 0's
run measured 3,504 B of stack room at a module's call, and the first scan run left 1,140 B
untouched, so the 160 B the row moved onto the stack still leaves about 1 KB. 2 m plus 70 cm
440-445 fit in one run (800 bins, 128 columns).

Until 2026-11-07 08:40 the row was in the scratch too. CI's aae8956 build (3,476 B) then had room
for only 108 columns, so runs with that binary need `--cols 96`.

## Tests

`tests/test_k6_scan.py` runs the module on the emulated M0 against a receiver model with Phase
0's ramps (wide and narrow), ±1 dB read noise and a filter skirt. It checks a repeater keyed three
times (three `H` lines at the right length and level), a weak station 5 kHz off the coarse raster
(refined onto its channel), a carrier that never moves (a spur, then no more revisits), the
waterfall column, two segments in turn, the bin cost from the `S` lines, the key, bad blocks, and
both memory limits.

## First bench run (2026-11-07 08:03 UTC)

Image keyed-lr-45c23e0 on the radio, nothing flashed; the module (d18bdc9, then still called
sweep) sent from hamlet, 2 m for 90 s, wide, 1.5 ms quick reads. All 28 chunks in one send.

- **403 bins a second**: 91 sweeps of 400 bins, 968-971 ms each, against the design's estimate of
  0.75 s and `plan`'s 1.52 s. A coarse bin costs about 0.92 ms beyond its 1.5 ms read: a tune
  and a register read take under 1 ms, not the 2.3 ms that rxmap's run time implied (rxmap's
  overhead is elsewhere: its lines and full reads).
- The module returned in 92.9 s with 1,140 B of stack never touched, so the floors on the stack
  leave a margin.
- One candidate, `T 144.000 n90 t0 -71 d1`: bin 0, hot on all 90 sweeps for a moment and gone at
  the revisit. 144.000 is a 24 MHz harmonic (rxmap saw those), but the R rows show it only at
  bin 0, right after the jump from 147.99 back to 144.00. Since then each sweep parks on its
  first bin for confirm_us before it starts; the next run tells the two causes apart.

## Second bench run (2026-11-07 08:19-08:26 UTC)

Same image, nothing flashed. The module came from CI run 42354372005 (aae8956, 3,476 B), which
had room for only 108 columns, so both runs used `--cols 108`. 29 of 29 chunks went in each
time with no resends, and the ARQ report was heard both times.

**2 m, 90 s:** `Z sw=90 bins=36000 ms=90131 bps=399 hot=1`, so the speed held at 399 bins/s.
**Bin 0 still lights up on every sweep, park or not.** The line is `T 144.000 n89 t0 -71 d1`,
with 89 overs of 0 ms each. It reads -71 at the confirm, 12 ms after the tune, and is gone at
the revisit. So it isn't the 4 MHz jump, since the park now comes between the jump and the read.
What comes just before bin 0 every sweep is the burst of serial lines (R and S, about 150-200 B,
13-17 ms at 115,200 baud). If the UART is still sending during the park and the read, that
would explain it. The next run tests this with `--no-rows`.

**70 cm (70a, 440-445), 120 s, CW ID at 40.7 s:**

- **The CW ID was seen.** `N 440.850 -22 fl-106 b68`, then `H 440.850 7955 -22`: one over of
  8.0 s at the IC-7100's level, as it should be. Bins next to it were also flagged during the
  key-up (440.862 at -59, 440.900 at -81, 440.912 at -74), as was 444.6-444.98 at -105 to -117.
  None of them stayed up.
- **The segment floor started 35 dB high:** `fl-106` at the ID, then -141/-143 from the next
  confirm on. The floors are read right after the module waits out the upload's own -22 dBm
  carrier on 440.850. On 70 cm the receiver seems not to have recovered 0.2 s later. The wait is
  now 1 s.
- **With the floor right, a weak steady signal showed up:** nine bins in 440.000-440.525 at -104
  to -106, up for the remaining 91-95 s. 440.000 and 440.525 were marked spurs. That's about
  35 dB over the floor, so it's real energy, most likely from the bench (the IC-7100 or the PC
  next to the K6). A drive away from the bench will tell.
- **Bins a second fell to 114** once the confirms started: `Z sw=36 bins=14400 ms=125773
  bps=114 hot=9`. That's about 3.5 s a sweep instead of 1. The emulator puts nine up entries at
  about 20 % of the time, so most of the loss must be confirms that never make a hit. A bin whose
  quick read passes the noise test but whose confirm fails now has its quick floor raised to 3 dB
  under that read. It is confirmed again only if it gets louder, or after its floor has fallen
  back. In the emulator, with quick reads that jump 4-9 dB on a third of the bins, that takes
  the rate from 276 to 345 bins/s (`test_quick_reads_that_jump_are_confirmed_less_and_less`).

The logs are on hamlet in `~/sotto-work/runs/explorer-scan-2/`.

## Next bench run

Receive only, nothing flashed, with the module built from the branch head (3,480 B, 136 B of
scratch to spare):

1. `run 2m --seconds 60 --no-rows`: if bin 0 is clean without the R lines, the serial burst is
   the cause, and the fix is a park long enough for the UART to drain.
2. `run 70a --seconds 120` with the CW ID about 40 s in, with the serial log started *before* the
   upload so the `F` line is in it. Check whether the F floor now reads about -141, the `c`
   counts on the S lines, and bins a second.
