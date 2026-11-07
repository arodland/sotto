# K6 repeater table (explorer Phase 2)

`c/k6/rt.[ch]` is the explorer's saved-repeater table in the K6's SPI flash, built to the design
settled with Jeff (the explorer design doc, "Repeater table in SPI flash"). It's MIT C with no
library calls and no writable statics, 2.8 KB on the M0, so it can go into the EXPLORE core or a
RAM module. It reaches the flash only through `rt_flash_t` (read, program a page, erase a 4 KB
sector), so the same code runs here against `rt_host.c` and on the radio against the core's SPI
driver.

**Not on the radio yet.** A module can't reach this region today: API level 4 only lets it read
and write its one calibration sector. The table needs either the EXPLORE core or a module API
with flash access to 0x0F0000-0x0FFFFF (see "API level 5" in `docs/k6-explorer-next.md`).
Before the first write, `rt_mount` checks the region, and Jeff's SPI backup should read 0xFF
there.

## Where and how

- **Region.** 0x0F0000-0x0FFFFF: 16 sectors of 4 KB, 64 slots of 64 bytes each. Aura's last data
  (the SSTV image) ends at 0xED200, and sotto's codebook starts at 0x100000.
- **Log.** An edit appends the new version of an entry (same `id`, next `seq`, the next global
  `wseq`) and marks the old version replaced. Editing never erases a sector.
- **Spare.** One sector is always kept erased. When no other slot is free, the sector with the
  most dead slots has its live records copied into the spare, and then it's erased and becomes
  the new spare. Once every slot has been used, that's about one erase per 50-60 edits (the test's
  3,000 edits on 300 entries took about 40). A sector takes about 100,000 erases.
- **Capacity.** 15 × 64 = 960 slots. With 256 live entries there's plenty of room.

## Record

| Bytes | Field | Notes |
|---|---|---|
| 0-1 | state | low byte FF free, FE being written, FC valid, F8 replaced, F0 deleted; high byte FF, or a compaction's mark (E0\|V copying, C0\|V copied, 80\|V done) |
| 2-3 | seq | the entry's version |
| 4-7 | rx_hz | |
| 8-11 | tx_off | Hz, signed, 0 = simplex |
| 12-15 | rx_tone, tx_tone | CTCSS in 0.1 Hz, or DCS \| 0x8000; 0 = none |
| 16-17 | flags | wide, offset sure, tone sure, name typed, call typed |
| 18-19 | best_rssi, score | dBm + 200; the explorer's grade |
| 20-23 | last_heard | boot count << 16 \| minutes since boot |
| 24-39 | name | 16 characters |
| 40-49 | call | 10 characters, as heard |
| 50 | call_flags | portable, mobile, repeater, from CW, decoded twice |
| 51 | cw | tone index (600 + 50 i Hz) in bits 3:0, wpm / 3 in bits 7:4, 0 = never decoded |
| 52 | group | scan group 0-15 |
| 53 | spare | |
| 54-55 | id | the entry, 1-65534, never reused |
| 56-59 | wseq | write sequence, newest wins |
| 60-63 | crc | CRC-32 of bytes 2-59 (not the state word, which changes after the write) |

Two changes from the design doc's table: bytes 54-59 now hold the entry `id` and the write
sequence (the log needs both to tell versions apart), and the CRC skips the state word.

## Power cuts

The state word only ever loses bits, one 2-byte write a step. A new record is marked "being
written" first, then its body goes in, then it's marked valid. Only after that is the old version
marked replaced. `rt_mount` repairs whatever a cut leaves:

- a slot that never reached valid: dead, and cleared by the next compaction;
- an edit cut after the new version but before the old was marked: the newest record by `wseq`
  wins, and the older one is marked;
- a compaction cut while copying (copies marked E0\|V): the source sector is untouched, so the
  copies go;
- a compaction cut after the copy (C0\|V): the source is erased again (it may be half erased) and
  the copies become the records.

`tests/test_k6_rt.py` cuts the power at every flash operation of an edit and of a compaction
(that write half done, that erase half done, everything after it failing). After each cut it
remounts, checks that no entry is lost or doubled, and checks that the table still takes edits.
It also runs 3,000 random edits through about 40 compactions.

## The region check

A first mount of a region that holds anything that isn't a table record returns
`RT_ERR_NOT_BLANK` and writes nothing. A forced mount (`force`) takes the region over: slots that
aren't records are cleared and the first compaction erases them. This keeps the table from ever
landing on someone else's data.

## CW ID to table

This is the policy Jeff set (2026-11-07): "auto if blank, ask if changed". The CW ID listener's
`CWID ok <call>` result goes to `rt_cw_apply(rx_hz, tol, call, flags, tone, wpm, confirmed)`:

| Stored entry on that frequency | Result | Writes |
|---|---|---|
| none | `RT_CW_NO_ENTRY`: the explorer keeps the call with its RAM candidate | no |
| a blank call | `RT_CW_FILL`: call, call flags (+ from CW), byte 51 | yes |
| the same call | `RT_CW_SAME` | no |
| a different call (typed or decoded) | `RT_CW_ASK`: show the old and new calls, wait for a key | no |
| the same, after the key press (`confirmed`) | `RT_CW_CHANGED` | yes |

`rt_cw_check` gives the same answer without writing, which is what the screen uses to decide
whether to ask. Writing a call clears "call typed". Nothing else from the listener writes flash.
