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
5. **Revisit.** Hits go on a hot list of 11 entries, visited between blocks of 32 bins, every
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
| `H 146.940 2030 -71 n3` | an over ended: centre, ms up, level, overs so far |
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

The module is 3.4 KB of the 4 KB store. The scratch RAM after it holds the state (180 B), the hot
list (11 × 28 B) and the row (up to 148 columns). The floors, one byte a bin and up to 1,024 bins
in all, live on the stack: Phase 0's run measured 3,504 B of stack room at a module's call, and
this module takes about 1.2 KB of it. So 2 m plus 70 cm 440-445 fit in one run (800 bins).
`K6 MOD ret ... stack_free=` on the first run will show the real margin.

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

## Next bench run

Receive only, nothing flashed: `run 2m --seconds 90` again (does bin 0 still show?), then
`run 70a --seconds 120` with the IC-7100 keying its CW ID on 440.850 (it falls inside 70a) as the
known signal. What to read off:

- the real bin cost and bins a second (`Z`, `decode`), against the design's 0.75 s for 2 m;
- whether the CW ID shows as one entry with one `H` line per ID, at the IC-7100's level;
- whether `stack_free` on the `K6 MOD ret` line leaves a margin;
- what the K6 itself puts on 2 m and 70 cm (spurs: rxmap found 13, 6 and 25 MHz harmonics).
