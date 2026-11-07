# K6 explorer: what's built, and the road to Phase 3

The K6 signal explorer's design lives in the "K6 signal explorer" Claude Doc (the master plan).
This page is the engineering view of where the pieces stand on 2026-11-07, what keeps Phase 2
off the radio today, and the steps to the EXPLORE core image (Phase 3). The phases are the
master plan's: 0 settle-time meter, 1 fast sweep, 2 explorer core, 3 core image and library,
4 field test.

## The pieces

| Piece | Where | Size on the M0 | State |
|---|---|---|---|
| Phase 0 settle meter | `c/k6/mod/settle.c` | RAM module | done, on air 2026-11-06 |
| Fast scan (Phase 1) | `c/k6/mod/scan.c`, `tools/k6_scan.py`, docs/k6-scan.md | 3,460 B module | on air: 403 bins/s, a 2 m sweep in 0.97 s |
| CW ID decoder | `c/k6/cwid.c`, `c/k6/mod/cwlisten.c`, docs/k6-cwid.md | 2.8 KB (cwid.c), 3.95 KB module | on air: 6 of 6 IDs exact |
| Repeater table | `c/k6/rt.[ch]`, docs/k6-repeater-table.md | 3.0 KB | host tests only; needs flash access (below) |
| Candidate list and grades | `c/k6/explore.[ch]` | 1.5 KB, 48 B a candidate | host tests only |
| Replay | `tools/k6_explore.py replay LOG...` | host | reads scan and cwlisten logs, prints the ranked list |

All of the C is MIT, with no division, no library calls and no writable statics, so each part
can go into a RAM module or the core unchanged. Each one builds for `thumbv6m` in CI with no
undefined symbols (the M0 test in each `tests/test_k6_*.py`).

What the scan gives the explorer (one line per event, docs/k6-scan.md):

| Scan line | Explorer call |
|---|---|
| `H f ms lvl aT`: an over ended | `ex_over(hz, T, ms, lvl)` |
| `X f lvl`: a spur | `ex_spur(hz)` |
| `CWID ok CALL ... rx=HZ` (cwlisten) | `ex_cw(hz, call)`, and `rt_cw_apply` for a saved entry |
| (not built) an input heard at rx + offset | `ex_input(hz, off)` |
| (not built) a tone read on the output | `ex_tone(hz, tone)` |

`k6_explore.py replay` does that wiring on the PC today. That's the master plan's "PC replay":
the scoring can be tuned on logs with no RF.

## What keeps Phase 2 off the radio

**Flash.** A RAM module can read and write only its one calibration sector (API level 4,
`cal_read` and `cal_write` at 0x10F000). The repeater table needs 0x0F0000-0x0FFFFF.

**Room.** A module has 4,072 bytes of code plus scratch. The scan (3.46 KB), cwid plus its
listener (3.95 KB) and rt (3.0 KB) each fill most of one, so no two of them run together as a
module. On the radio, "a CW ID fills in the saved entry's call" needs both cwlisten and rt at
once, so that has to wait for the core. Before that, the host can carry the result from one
module to the next.

### API level 5 (proposed)

These calls are appended to `k6_api_t`, so level 1-4 modules keep working:

| Call | What it does |
|---|---|
| `fl_read(addr, buf, n)` | reads SPI flash anywhere from 0x0F0000 to 0x1FFFFF (the table, the codebooks, the calibration); n, or -1 |
| `fl_prog(addr, buf, n)` | programs up to 256 bytes inside one page, 0x0F0000-0x0FFFFF only; 0, or -1 |
| `fl_erase(addr)` | erases the 4 KB sector at addr, 0x0F0000-0x0FFFFF only; 0, or -1 |

Rules in the firmware, not left to the module:

- **The region is fixed.** Writes and erases outside the table's 64 KB are refused, so a module
  can't touch Aura's data, the codebooks or the calibration.
- **Never while keyed.**
- **At most 32 erases a run.** A compaction erases one sector, and a mount repair at most two,
  so 32 is far more than a working module needs. A runaway loop gets stopped long before it
  wears a sector out (a sector takes about 100,000 erases).
- The three calls are exactly `rt_flash_t` (`c/k6/rt.h`). A module passes them straight to
  `rt_mount`, and `rt_host.c` simulates the same calls for the tests.

The firmware side is the Aura patch's SPI driver, which already does reads (the codebook) and
the calibration sector's erase and program. It needs the address checks, the erase counter and
three entries in the API table. It's a flash of the core image, so it rides on the next one
that's needed for another reason. On the first mount, `rt_mount` checks the region and refuses
anything that isn't blank or a table, so a wrong guess about Aura's map costs nothing.

### Modules that fit once level 5 is there

- **`rtcw`**: rt plus about 300 B of glue. The parameter block carries `rx_hz, call, flags, tone,
  wpm, confirmed`. It runs `rt_cw_apply` and says the result: `RT cw FILL id=7`, `RT cw ASK old=
  W7XYZ new=W7ABC`, and so on. The host runs cwlisten, reads `CWID ok`, then sends `rtcw`. On the
  bench this tests Jeff's "auto if blank, ask if changed" rule end to end, with the key press
  done by sending `rtcw` again with `confirmed` set.
- **`rtls`**: mounts the table and says one line an entry. It never writes, unless the mount
  repairs something.
- **`rtsave`**: one record from the parameter block (what `ex_to_rt` fills) through `rt_put`.
  That's the explorer's Save until the core has a screen.

## Phase 2 cues still to build

### Input heard at the offset (the strongest cue)

The idea: while an output is up, hop once to its input and read RSSI there. A user near the
K6 shows up at the input. Plan:

- When a hot entry starts an over and the band plan gives it an offset, the scan makes one
  extra `full()` read at rx + offset, then tunes back. One over costs one tune plus a 12 ms
  (wide) read.
- "Heard" means at least `hit_db` over the input's own quick floor, if the input falls inside a
  segment. Otherwise it's compared with the segment's settled floor (`sfl`).
- Two hits on different overs fix the offset: `I f off lvl`, then `ex_input`. If none of the
  usual offsets is heard, try the other sign once.
- Room: about 100 B of code. The scan has 152 B of row room left at 128 columns, so it fits, but
  only just. In the core it's free.
- Bench: one IC-7100 can't be both the output and the user. This needs a real repeater, or a
  second transmitter on 440.850 + 5 MHz. The emulator test can model it (two carriers keyed
  together).

### Tone on the output

The plan: dwell up to 0.4 s on an over with no tone yet, and believe a CTCSS or DCS reading only
when two reads agree, as Aura's ScanQT does. On the BK4819 the scan results are REG_68 (CTCSS)
and REG_69 (CDCSS), as the UV-K5 firmware reads them. **Not confirmed on this chip (0x4819)**,
and they aren't in the register map yet (k6-fd6818-regmap). Steps:

1. Read Aura's ScanQT driver code for the exact set-up, since that code works on this chip.
2. Write a probe module (level 2, like cwprobe): the IC-7100 keys 440.850 with a 100.0 Hz tone,
   then 123.0 Hz, then a DCS code, and the module prints REG_68/69 every 50 ms.
3. Add what it finds to the register map before anything else uses it.
4. Then add the dwell to the scan or the core.

The dwell takes the sweep away for 0.4 s, so do it once per candidate, not once per over.

### Short regular overs (IDs)

This one is built. `ex_over` counts short overs (2-15 s) that come 5-20 minutes after the last
short one. The scan's `H` lines carry each over's start time (`a` in module ms) for this.

On the bench, Jeff's ID keeper sends AG7EW every 9 minutes on 440.850. That's inside the
440-445 output band, so a 30-minute 70 cm scan should grade it **R**: it's at one level (+3), in
the output band (+2), and IDs regularly (+1). That's a false positive, and an honest one, since a
simplex station that IDs on a schedule from a fixed site looks just like a repeater to these
cues. The input-hop cue is what tells them apart. It's also a good first replay test of the
whole chain on real data.

## The band plan

`ex_plan_us` (c/k6/explore.c) has nine rows from memory: ARRL 2 m and 1.25 m outputs and
simplex, 70 cm 440-445 with +5 MHz, and the 70 cm simplex calling range. Before a release, check
them against:

- ARRL's current 2 m and 1.25 m band plans;
- the WWARA 70 cm plan (Jeff's area, and the master plan's region), including whether 440-442
  has outputs here;
- the IARU Region 1 rows from the master plan, which aren't in the table yet (no region
  setting yet).

A wrong row costs at most 2 or 3 points on a candidate and a wrong offset guess. Try's
second press (the other sign) and the input-hop cue both correct it.

## Phase 3: the EXPLORE core image

What goes into the core, in order:

1. **API level 5** (above), on the next image that's flashed anyway.
2. **The scan engine in the core.** Same `scan.c` loop, called from the main loop a block of 32
   bins at a time instead of owning the CPU. The `R`/`S` lines become the waterfall rows.
3. **explore.c and rt.c linked into the core**, about 4.5 KB of flash together. Explorer RAM:
   64 candidates × 48 B = 3 KB, the floors 1 KB, the hot list 280 B and the waterfall 768 B.
   That's 5 KB against the plan's 4.5 KB. If it doesn't fit, a 28 B candidate gets there: times
   in ×1024 ms units as u16, the offset in 100 kHz as i8, levels as u8, and the call moved to a
   side table of 8 calls.
4. **cwid.c in the core**, with "Listen for ID" from the list screen. The wiring:
   `CWID ok` → `ex_cw`. Then `rt_cw_check`: on `RT_CW_ASK`, show the old and new calls and call
   `rt_cw_apply(..., confirmed = 1)` on the key; on `FILL`, `rt_cw_apply` right away.
5. **Screens** (Rust, in the Aura patch, GPL-3): the spectrum and waterfall, and the list.
   Save goes through `ex_to_rt` then `rt_put`. Try sets the VFO in RAM.
6. **222-225 MHz in the FCC TX range** (Try on 1.25 m). Check the K6's power and spurs there
   before enabling it, and ask Jeff first, since it's outside 70 cm.

Each of these is a host-tested C part or a Rust screen, so the first flash of the core image
can be one `k6_update.py` run, as with the pager images.

## Next bench steps (70 cm only, receive except for the CW ID)

1. **Second scan run** (in progress, image on the radio, no flash): 2 m for 90 s, then 70a for
   120 s with a CW ID 40 s in. This confirms the bin-0 park and the off-round floor reads.
2. **A 30-minute 70a scan with the ID keeper running.** Feed the serial log to
   `k6_explore.py replay` and check the ID-rhythm cue on real data. Expect AG7EW on 440.850 to
   grade R, as above.
3. **Tone probe** (above), receive only, the IC-7100 sending tones on 440.850.
4. After level 5: `rtcw` against the ID keeper. The first run fills a blank entry. A second run
   with a different call asks, and a third with `confirmed` writes.
