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
hot list (10 × 28 B), which leaves about 84 B to spare with this machine's clang and about 60 B
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

Nothing flashed by this thread (which image was on the radio: to confirm). The module came from CI run 42354372005 (aae8956, 3,476 B), which
had room for only 108 columns, so both runs used `--cols 108`. 29 of 29 chunks went in each
time with no resends, and the ARQ report was heard both times.

**2 m, 90 s:** `F0 -94 q-135`, `Z sw=90 bins=36000 ms=90131 bps=399 hot=1`. The speed held at
399 bins/s. Bin 0 still lights up on every sweep, park or not: `T 144.000 n89 t0 -71 d1`. The
segment floor read -94, the same as the first run's `F -93` (the bench's floor; see below).

**70 cm (70a, 440-445), 120 s, CW ID at 40.7 s:**

- **The CW ID was seen.** `N 440.850 -22 fl-106 b68`, then `H 440.850 7955 -22`: one over of
  8.0 s at the IC-7100's level, as it should be. Bins next to it were also flagged during the
  key-up (440.862 at -59, 440.900 at -81, 440.912 at -74), as was 444.6-444.98 at -105 to -117.
  None of them stayed up.
- **The segment floor started 35 dB high:** `F0 -106 q-140`, and `fl-106` at the ID, then -141/-143 from the next
  confirm on. The floors are read right after the module waits out the upload's own -22 dBm
  carrier on 440.850. On 70 cm the receiver seems not to have recovered 0.2 s later. The wait is
  now 1 s.
- **With the floor right, a weak steady signal showed up:** nine bins in 440.000-440.525 at -104
  to -106, up for the remaining 91-95 s. 440.000 and 440.525 were marked spurs. That's about
  35 dB over the floor, so it's real energy, most likely from the bench (the IC-7100 or the PC
  next to the K6). A drive away from the bench will tell.
- **Bins a second fell to 114**: `Z sw=36 bins=14400 ms=125773 bps=114 hot=9`. The S lines
  explain it. Sweeps 0-22 took 952 ms with no confirms. After the floor fell, from sweep 29 on,
  each sweep took 13.2-14.9 s with `c106-120` and `h` equal to `c`. Every one of those bins was a
  *hit*: about -105 dBm, against a segment floor of -142. The hot list was full of the first
  nine weak carriers, all still up, so every other hit was refined (about 80 ms of narrow reads)
  and then dropped, on every sweep. The emulator shows the same thing: 20 weak carriers in a
  2 m test segment took it to 71 bins/s
  (`test_a_list_full_of_weak_carriers_stays_fast_and_takes_a_repeater`).

  **Fixed:** a hit first checks for a place on the list and isn't refined if there's none. A
  hit with no place is taken into its bin's quick floor, so it costs nothing until it gets
  louder. A place is a free entry, else the one down longest, else the weakest entry (one that's
  up, or a spur) if the new hit is `hit_db` louder. So a list full of weak carriers still takes a
  repeater that keys up. In the emulator that keeps 369 bins/s and records the repeater's over.
  Also, a confirm that misses now raises that bin's quick floor to 3 dB under its read, which
  helps quick reads that jump about.

The logs are on hamlet in `~/sotto-work/runs/explorer-scan-2/`.

## Third bench run (2026-11-07 08:37 UTC)

The module was 9c296dd, CI run 42355911841 (3,516 B, sha256 c1c0e42a...). It had the row on the
stack, the 1 s settle and `--no-rows`, but not yet the full-list fix. Logs:
`~/sotto-work/runs/explorer-scan-3/`.

- **2 m, 60 s, `--no-rows`:** `F0 -94 q-136`, `Z sw=62 bins=24800 ms=60978 bps=406 hot=1`,
  stack_free 1,140 B. Bin 0 lit up just the same with no R lines: `N 144.000 -72 fl-94 z1 b0`,
  then 50 overs, `T 144.000 n50 t37 -71 d1`. So the serial burst isn't the cause. 144.000 was up
  37 s of the 60, which makes it a real carrier rather than an artifact. It's most likely 6 × 24
  MHz (rxmap found the 24 MHz harmonics), and it simply falls on bin 0. In the first run, at
  `t0`, the revisit never caught it up; with the per-bin floors it now does. It will turn into an
  `X` spur after `spur_s` (60 s), once a run is long enough.
- **70 cm, 120 s, CW ID at 41 s:** `F0 -106 q-141`, then sweeps 0-20 at 952 ms with no
  confirms. The ID was caught: `N 440.850 -23 fl-140 z2 b67` and `H 440.850 7891 -23`. After it,
  the -105 dBm carriers at 440.000-440.200 came up again, and sweeps 28-34 took 12.6-14.7 s with
  101-119 hits each. That's the full-list slowdown again, as expected for this build:
  `Z sw=35 bins=14000 ms=123914 bps=112 hot=7`.
- **The floors weren't wrong:** -94 on 2 m and -106 on 70 cm are the bench's real floors. rxmap
  measured median floors of -91 in the VHF filter and -108 in the UHF filter at the bench
  (docs/k6-rxmap.md), most likely noise from the PC. All 8 reads of each F line agree to within
  4 dB, and the first run's log says `F -93` as well. (I had misremembered it as -137, Phase 0's
  quiet-channel figure.)
  The K6 is on the same image (keyed-lr-45c23e0, flashed 07:26) for all three runs.
- **What really went wrong on 70 cm:** the floor fell from -106 to -140/-142 during the CW ID. A
  -22 dBm carrier desenses the other bins, so a confirm next to it reads about -140, and the
  floor used to jump straight to any lower confirm. After that, the bench's own -105 dBm noise was
  37 dB "over the floor" everywhere: 101-120 hits a sweep, and with a full list, 14 s a sweep.
  **Fixed:** a confirm that fails moves the floor 1 dB toward its read, and one more than 6 dB
  under it is ignored. The emulator now has a bench model for this: a -106 floor, desense to
  -140 and splatter on the quick reads next to a -22 dBm carrier
  (`test_a_strong_carrier_s_desense_doesnt_drag_the_floor_down`). It fails on the previous code
  and passes now. The cost is that a real, sudden drop of more than 6 dB in the floor isn't
  followed until the next run. The per-bin quick floors still follow it.
- **The 1 s settle** stays. It costs nothing and the evidence for it is gone, but a strong upload
  carrier is still a reason to wait.

## Fourth bench run (2026-11-07 08:49 UTC)

The module was 55835eb (the full-list fix, but not yet the floor fix), CI run 42356625870, 3,516 B.
Logs: `~/sotto-work/runs/explorer-scan-4/`.

- **2 m, 60 s, `--no-rows`:** `F0 -93 q-135`, `Z sw=61 bins=24400 ms=60150 bps=405 hot=1`.
  144.000 again on every sweep (`T 144.000 n60 t0 -71 d1`), one H line a sweep.
- **70 cm, 120 s, CW ID at 40.7 s:** `F0 -105 q-140`. The ID was caught (`H 440.850 7782 -23`).
  The floor was still dragged down, -114, then -116, then -143 by sweep 25, as expected without
  f16ff0c. The bench noise at 440.000-440.225 then came in as hits. **But the full-list fix held
  on air:** one slow sweep of 3.6 s (123 hits, the list filling), then sweeps 30-103 at
  1,093-1,176 ms with 0-5 confirms. `Z sw=107 bins=42800 ms=121436 bps=352 hot=10`, against 112
  without it. Something at 440.925, -91 dBm, stayed up from 08:52:57 to the end, with nobody
  keying. 440.850 had dropped off the T list by the end: it was down, and a newer entry took its
  place, but its H line is in the log.

## Fifth bench run (2026-11-07 09:03 UTC): the floor holds

The module was f16ff0c, CI run 42357086797, 3,532 B. The K6 was on the new pager image fe25ec8
(PR #108); the scan's environment is the same. 70 cm for 120 s. There were two CW IDs: the ID
keeper's at 0.5 s in and ours at 40.5 s. Logs: `~/sotto-work/runs/explorer-scan-5/`.

- **The floor held at -105** (`F0 -105 q-141`) on every N line, through both -22 dBm IDs. The
  bench noise at 440.0-440.2 never came in as hits.
- **Both IDs became one entry with two overs:** `H 440.850 7852 -23` and `H 440.850 8653 -22`,
  then `T 440.850 n2 t17 -22 d1`.
- **Speed:** `Z sw=112 bins=44800 ms=120286 bps=372 hot=4`. Sweeps take 1,008 ms when it's
  quiet. Right after each ID they took 1.2-2.6 s with 28-133 confirms and no hits, as the
  desensed bins recovered. stack_free was 1,060 B.
- **The carrier's skirts are listed as their own entries:** 440.862 at -58, 440.900 at -79 and
  440.912 at -73 to -79, each up only while the ID is. The K6 hears a -22 dBm carrier 12.5-62.5
  kHz either side. The explorer now marks those as skirts (`c/k6/explore.c`, EX_SKIRT_DB 25 dB
  within EX_SKIRT_HZ 100 kHz, ending within 2 s of the louder over). They rank last.

So on the bench, Phase 1 now does what it should: the right floor, the CW ID found as one
repeater-like entry, and about 1 s a sweep.

## Sixth bench run (2026-11-07 13:14-13:44 UTC): 30 minutes of 70 cm

The module was mod-scan.bin from CI on main 5cde22e (3,532 B), on the K6's usual image 4709d7e.
It swept 440-445 MHz in 12.5 kHz bins for 1,800 s: `Z sw=1450 bins=580000 ms=1800805 bps=322
hot=10`, sweep median 1,221 ms, settled floor -104 dBm, stack_free 1,052 B, 10 candidates and no
spurs. Logs: `~/sotto-work/runs/explorer-scan-6/` (serial log, replay.txt, wf.svg, hits.csv).

- **Real signals: two.** 440.925 at -90 to -92 dBm keyed every 2-5 minutes (13:17, 13:19, 13:29,
  13:34, 13:39; 7 overs, graded r), and 442.025 at -82 once (13:29, 2.6 s). Neither is ours.
- **Everything around 440.850 was us.** 440.837, .850, .862, .887, .900 and .912 line up to the
  second with the upload at 13:14 and the keeper's ID at 13:23: the IC-7100 and its skirts.
  Only 440.862 was marked as a skirt. The 13:23 ID's first read caught the IC-7100 coming up
  (-72 dBm, against -23 at 13:14), so the others weren't 25 dB under it. xscan now reads an
  over's level again when it gets louder. The skirt cue itself still counts only overs that end
  with the louder one's, so a skirt that flickers on and off isn't caught: that's still open.
- **The floor stuck low.** During the 13:23 ID, 440.0-440.1's floor was dragged from -104 to
  -119 dBm, and with the band quiet afterwards (c0 on every sweep) nothing brought it back.
  440.037, .062 and .112 read 13 dB "up" and stayed keyed for the last 1,232 s, graded `?`.
  The EXPLORE sweep (c/k6/xscan.c) now reads each segment's floor again every 60 s and lifts any
  bin's floor more than 6 dB under it (tests/test_k6_xscan.py drags the floors 15 dB down and
  sees every phantom end). The scan module still has the old floor logic.
- **The report was missed, then heard on the resend.** `k6_upload.py listen()` decoded the whole
  recording every second, which after 30 minutes was too slow to reach the 13:44:21 report
  before its window closed. It now decodes only the last minute (`tail_reports`), and
  `Recorder.audio()` reads from an offset instead of loading the whole file.

## Next bench run

Away from the bench: a 30-minute 70 cm scan somewhere quieter than the PC, fed to `k6_explore.py
replay`. That also gives the ID keeper's 9-minute rhythm and lets 144.000 turn into an `X` spur
on a 2 m run of 10 minutes or more.
