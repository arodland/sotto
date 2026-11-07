# EXPLORE: the K6 signal explorer app (Phase 3 build, Phase 4 field test)

EXPLORE finds likely repeaters by listening. The design is the "K6 signal explorer" Claude Doc,
the master plan, and the engineering road is [k6-explorer-next.md](k6-explorer-next.md). This page
covers the app as built on 2026-11-07, the image it runs in, and the field test.

The radio only listens. EXPLORE never transmits, and PTT does nothing while it's on screen.

## The image: `aura-explore.bin`

The explorer's C is about 13 KB (the sweep 4.0, the repeater table 3.0, the CW ID decoder 2.8,
the candidate list 1.75 and Listen for ID 1.4), with 4 KB of Rust screen on top. The pager images
have 17-18 KB free, so it doesn't fit beside the voice decoder. Full Aura has 7.5 KB free and
won't fit it either until SSTV and the satellites move out (the master plan's Phase 3). So
EXPLORE has an image of its own:

| | aura-explore | aura-pager-lr (for comparison) |
|---|---|---|
| flash | 97,632 of 122,880 B (25.2 KB free) | 105,680 B (17.2 KB free) |
| static RAM | 9,668 B (8.0 KB of stack) | 10,580 B (7.1 KB) |
| starts in | EXPLORE | VOICE MSG |
| voice | none (the decoder is left out) | sotto 1200 |
| RAM modules | yes, API level 5 | yes, API level 5 |

It is the pager-only image built with the cargo feature `explore` and a library without the
voice decoder (`K6_VOICE=0 sh tools/k6_build.sh`; `c/k6/sp_novoice.c` stands in for it).
VOICE MSG still receives packets, runs RAM modules and takes control packets, but plays no
voice. EXIT from VOICE MSG goes back to EXPLORE. To play voice again, flash a pager image.
CI builds it as `aura-explore.bin` (`firmware/k6-aura/build.sh`, `EXPLORE=0` skips it).

EXPLORE has no RAM of its own. It borrows the 4 KB message store for the sweep state and 64
candidates, and the PWM ring for the floors, the waterfall row and Listen for ID. RAM modules
and the 4FSK session borrow the same memory. Leaving EXPLORE clears the store for messages.

## How it works

- **Sweep** (`c/k6/xscan.c`): the scan module's two-stage sweep (docs/k6-scan.md, 372-405 bins/s
  on air), cut into steps of at most 32 bins, so the keys, the screen and the power switch get
  the CPU back about every 80 ms. Each over that ends goes to the candidate list and out on the
  serial report as the module's `H` line. `k6_explore.py replay` and `k6_scan.py decode` read a
  serial log of the app the same way they read the module's (tests/test_k6_xscan.py replays one
  and gets the same list). A step also ends after a new hit or 6 confirms, so it never holds
  the CPU much over 100 ms. Each segment's floor is read again every minute (`F` lines): a
  strong carrier pulls the floors down while it's up, and on the bench they stayed 15 dB low
  for 20 minutes after our own ID (explorer-scan-6, docs/k6-scan.md). An over that gets louder
  after its first read takes a full read again, so it carries its real level.
- **Candidates** (`c/k6/explore.c`): the grades R / r / s / ? from the cues in
  k6-explorer-next.md: same level each over, the band plan, the ID rhythm, a CW ID, spurs and
  skirts.
- **Listen for ID** (`c/k6/xlisten.c`): cwlisten's tone detector set-up and decoder (6 of 6 IDs
  on air), a step at a time. A call needs two decodes alike.
- **Automatic Listen for ID** (Jeff's rule, 2026-11-07): when a candidate with no callsign keys
  up, the sweep goes and listens to it (quietly, with the speaker off). If several are up at
  once, it picks the one listened to fewest times (`ex_pick_id`), and every listen moves that
  one to the back, so two repeaters that ID together take turns and neither starves. It stays
  while the carrier is up, then 3 s more, 2 minutes at most, and then leaves 5 s of sweeping
  before the next listen. A call heard once is kept, and a second decode alike, on the same
  listen or a later one, confirms it. Spurs, skirts, simplex frequencies, ignored candidates and
  's' grades are never picked. Key 1 turns it off and on.
- **The table** (`c/k6/rt.c`): Save writes one 64-byte record in SPI flash at 0x0F0000-0x0FFFFF,
  through the same checks as module API level 5. A confirmed CW ID follows Jeff's rule: a saved
  entry with no call takes it at once, and a different saved call waits for MENU. With no saved
  entry, the call stays on the candidate in RAM until Save. Nothing else writes flash. On entry,
  the table mounts. If 0x0F0000-0x0FFFFF holds anything that isn't blank or a table, the screen
  says `TABLE AREA NOT BLANK`, and Save stays off.

### API level 5 (Jeff approved 2026-11-07)

`fl_read`, `fl_prog` and `fl_erase` are appended to `k6_api_t` (c/k6/k6_mod.h). `fl_read` covers
0x0F0000-0x1FFFFF. `fl_prog` (up to 256 B inside one page) and `fl_erase` (one 4 KB sector) work
only inside the table's 64 KB. Neither works while keyed, and each module run or EXPLORE session
gets at most 32 erases. Every erase goes on the serial report (`K6 FL erase 0xf3000 n=1`). The
lean images refuse all three. The first level 5 module is `rtls` (`mod-rtls.bin`, 2.1 KB): it
mounts the table and says one `RT` line per entry, so the table can be read over the air with
`k6_upload.py`.

## The screen and keys

The screen layout is Jeff's pick from the font sheet (2026-11-07, /mnt/project-files/k6-fonts/):
a header in the 6x10 font, dark on a lit bar (21 characters), then 7 candidate rows and a
footer in the 4x6 font (32 characters a row, so a row also shows its callsign once heard).
New repeaters are listed first. A thin rule separates them from the known ones under it: the
frequencies already saved in the table (read at the start, and added on each Save). The rows
under the rule move down 2 px. The selected row is drawn inverted, dark on a lit bar (Jeff's
idea: no '>' column). Grades: R likely a repeater (score 6 or more), r maybe one, ? not seen
enough yet, s simplex or a louder signal's skirt, x ignored with *. A spur (a birdie, or a carrier
that never moves) shows a little gull in place of the grade, and its footer says "birdie, not a
station". The level bar
(Jeff's "4 revised", 2026-11-07) is 2 px of loudest level, 5 dB a pixel from -120 dBm (21 px
full), over a dotted line for the score (10 points full).

The header (Jeff, 2026-11-07) has an icon pair that swaps every second. While sweeping, it shows
half-lidded eyes looking left, then right, followed by the band set and how many candidates
were found. While listening for a CW ID, it shows dit-dah / dah-dit, the frequency, and a watch
face at the right end whose wedge empties clockwise over the listen's time limit (2 min for the
automatic listen). The automatic listen keeps the list on screen, with an arrow in the call
column pointing at the row being listened to. On serial, a known candidate has flag 0x20 in its K6 CAND line.

```text
(eyes) 70CM  7 found       sweeping: band set, candidates (listening: dit-dah, MHz, watch)
R 146.940 -0.6 ====  W7ABC  grade, MHz, offset (S simplex), level bar, call
r 443.100 +5.0 ===         (the selected row is drawn inverted)
? 440.850 +5.0 ========
s 146.520   S  ======
-80 dBm  score 8  4 overs  the selected row: loudest level, score, overs
```

| Key | In the list |
|---|---|
| UP / DOWN | pick a row |
| MENU | Try: the sweep stops, the radio parks on the row with the speaker open (squelch open, so you hear the noise between overs). MENU or EXIT goes back |
| 0 | Listen for ID on the row: speaker open, up to 25 minutes, any key stops |
| 5 | Save the row to the table (`SAVED #3 (3 in table)`) |
| * | ignore or unignore the row (`x` grade, ranked last) |
| # | next band set: ALL (2 m, 1.25 m outputs, 70 cm 440-445), 2M, 70CM (440-450), 1.25M |
| 1 | automatic Listen for ID off or on (on at power-on) |
| 7 | the list and the saved table on the serial report (`K6 CAND`, `K6 RT` lines) |
| EXIT twice | VOICE MSG (the list is in RAM and goes when you leave or power off) |

On the Listen screen: `ID? 440.850 1:23` (or `AUTO`), the tone detector's state, the last text
decoded and the call so far. On a changed call: `NEW CALL`, the heard and saved calls, MENU
writes the new one and EXIT keeps the saved one.

## Serial report

Everything the app does is a line on the programming cable (115200 8N1), read-only as always:

```text
K6 EX enter cap=64 rt=0 live=0 repairs=0 xl=...
K6 EX band ALL bins=988 r=0
K6 MOD say F0 -93 q-135: ...          floors, as the scan module's
K6 MOD say N 146.940 -80 fl-93 b294   a new hit
K6 MOD say H 146.940 9050 -80 a61250  an over: the candidate list's input
K6 EX auto 146940000 listens=1        the automatic listener went there
K6 MOD say CWID ok W7ABC/R fl=4 rx=146940000 tone=800 wpm=20
K6 EX cw 146940000 call=W7ABC/R check=0 apply=0 id=0
K6 EX save 146940000 id=1 slot=0 live=1 erases=0/32
K6 CAND 0 R 146940000 off=-600000 tone=0 call=W7ABC/R score=8 overs=5 ...
```

## Phase 4: the field test

What we measure (the master plan's ground truth): run EXPLORE for 30 minutes or more somewhere
with repeaters in range, then count how many of the area's listed repeaters it graded R or r
(recall) and how many of its R entries are real (precision).

1. **Flash** `aura-explore.bin` (the hamlet session does it with `k6_update.py`, the module key
   written by `python -m modem.k6 keyimage`). The K6 boots into EXPLORE.
2. **Take it out.** A drive, a walk, or the house away from the bench PC (the bench floors are
   15-30 dB high from the PC's noise). Leave it on ALL, or press # for one band. The longer the
   better: a quiet repeater may key only every 10 minutes for its ID.
3. **Look and listen** whenever you like: MENU on a row to hear it, 0 to wait for its ID, 5 to
   save it. Saving is the only way a find outlives a power-off.
4. **Bring it back without turning it off**, plug in the cable with the logger running
   (`python -m modem.k6_harness listen --k6 PORT --log FILE`), and press 7.
5. **Score it** against a repeater list for the area (a RepeaterBook CSV export works, or a
   text file with one output frequency in MHz per line):
   `python3 tools/k6_explore.py score FILE --truth repeaters.csv`
   That gives the recall, the precision, every listed repeater it missed (and whether it heard
   it at all but graded it lower), and every R entry not on the list.

Expected false positive: a fixed simplex station that IDs on a schedule (like the bench's
AG7EW keeper on 440.850) grades R. The input-hop cue (not built yet) is what will tell them
apart.

## First bench check (2026-11-07 17:53-18:03 UTC, main ac2acdd)

The flash took one OTA reset and booted into EXPLORE. The K6 only received; the IC-7100 sent
three AG7EW CW IDs on 440.850. Log: `~/sotto-work/runs/explore-bench-1/serial.log` on hamlet.

- **Sweep:** 70CM (928 bins) at about 1.1 s a pass, floors -104 to -111 dBm.
- **Automatic Listen for ID:** it went to 440.850 by itself on the second and third IDs and
  decoded "AG7EW SOTTO TEST" both times. On its own it also found 440.925 (-93 dBm, the signal
  from explorer-scan-6) and listened to it.
- **What went wrong, fixed since:**
  - On ALL it listened to 144.000, the K6's own 24 MHz clock harmonic. Now the automatic
    listener only picks repeater output sub-bands, and a candidate within 2.5 kHz of n x 24 MHz
    is a spur from the start.
  - On the first ID it went to 440.900, the IC-7100's -81 dBm sideband next to the -24 dBm
    carrier, because 440.850 had no candidate until its first over ended. Now `xs_up` leaves
    out a carrier 20 dB under one up within 100 kHz, and `ex_pick_id` makes the candidate for a
    carrier keyed for the first time.
  - Two decodes of the same call on separate listens didn't confirm. The decoded call carried
    stack bytes after its NUL, and the app compared all 11 bytes. Now xlisten zero-fills the
    call and the app compares up to the NUL.
  - One held EXIT left EXPLORE and its key repeat opened it again from VOICE MSG, wiping the
    list. EXIT now counts presses, not repeats, both in EXPLORE and in VOICE MSG.

Then (18:19-18:22) Save wrote 440.850 to the table (2 of 32 erases), 7 dumped the list (7
candidates: 443.525 and 440.925 heard on their own, 440.862 graded s as the ID's skirt), and the
rtls module, sent by the IC-7100 (18 of 18 chunks in 23.6 s), read the table back from the K6:
`RT live=1 ... RT 1 440850000 5000000 T0 - r -21`. The call is `-` because of the confirm bug
above.

## Bench 3: false 2 m hits after a listen (2026-11-07 20:22-20:33 UTC, main 8a8c677)

The new header showed, but the K6 spent its time listening to 2 m noise. Log:
`~/sotto-work/runs/explore-bench-3/serial.log`. Each time an automatic listen ended, about 22
"new" 2 m carriers showed at once (146.900 to 147.990, -88 to -91 dBm, all against fl-101). The
listener sat 2 minutes on one of them, and then the same thing happened again. 2 m's real floor
was -93 (the first F0 line, at 20:27:14). Three faults locked together, all fixed in xscan:

- **The floor was 8 dB low and nothing refreshed it.** A segment's floor is read again only when
  no carrier 30 dB over it is up. Against the low floor, the 144.000 birdie (-71) counted as one,
  and a spur stays up for good, so 2 m went 4.5 minutes without a floor read (the 4-minute limit
  ended it). Spurs don't hold the floor read off now.
- **The sweep came back from a listen in the middle of a segment,** judging the rest of the
  segment against a floor from before the listen. The app now calls `xs_resume` after Listen,
  Try and automatic listens: the segment reads its floor again and sweeps from its start.
- **The quick floor read 40 dB low** (q-134) because each quick read came straight after the
  1/8-segment hop between floor points. The quick read now comes one bin over a settled full
  read, as in the sweep.

## Bench 5 (2026-11-07 21:35-21:40 UTC, main 32cd262)

The auto listen now stays up for a whole ID and ends 2-3 s after the carrier drops, and no floor
was held. But at 21:37:17 it went to 440.7375, a steady carrier only 10 dB over the floor (-103,
fl-113), sat the full 2 minutes on it (no CW), and missed the keeper's ID. Before, the 3 s bug had
cut such listens short. The tone detector can't decode at 10 dB anyway, so `xs_up` now leaves out
anything under 15 dB over its segment's floor (XS_PICK_DB): the sweep still lists it, the automatic
listener just doesn't go there.

Jeff's two follow-ups (21:48), from the same run's list: off the band plan's channel raster (25
kHz from 440.000 on 70 cm; `ex_plan_t.raster_khz`, none set on 2 m and 1.25 m yet) a carrier
needs 10 dB more (25 dB over the floor) before the listener goes there: every 12.5 kHz-offset hit
on the bench so far was junk, but WWARA has splinter channels, so it's a higher bar, not a ban.
And skirts are folded into the carrier beside them on the screen (`ex_view`): the run's exit
dump had four skirts of our -20 dBm ID (440.825 to 440.900) in seven rows. They stay in the
serial dump (key 7).

And his priority rule (21:54): when several carriers are up, the loudest goes first. `xs_up` gives
them loudest first, and `ex_pick_id` takes the first among those listened to fewest times, so a
carrier just evaluated moves to the back and the next loudest goes when it's up again.

## Bench 4 (2026-11-07 21:07-21:12 UTC, PR #122 head 7a591c0)

The floor fix and the birdie fix worked: 2 m read -92 to -94 every minute, no 2 m phantoms, and
144.000 was a spur on the first sweep (21:07:36), never picked. The AG7EW entry survived the
reflash, and an ID sent at 21:11:32 decoded clean. Log: `~/sotto-work/runs/explore-bench-4/`.
Two faults turned up around the keeper's ID at 21:09:24, both fixed in #122:

- **The automatic listen on 440.850 ended after 3 s with the ID still on air.** A carrier keyed
  for the first time has no over yet, so its level was 0 and the -18 dBm ID never counted as up.
  Now such a listen takes the level from its reads, and the loudest read sets what counts as up.
- **The floor read after that listen ran with the -18 dBm carrier still up,** came back 30 dB low
  (F2 -139 for -109), and five phantoms came up against it (444.550 at -116, which the next
  automatic listen went to). A floor read 15 dB or more under the last is now held once (`F2 held
  -109`) and tried again in 5 s; a floor that really dropped stands on the second read.

## Birdies (2026-11-07)

birdie-path-1 (hamlet `~/sotto-work/runs/birdie-path-1/`, hfprobe, RX only, the IC-7100 sending
PSK31 AG7EW TEST on 440.850 as a reference) read each birdie with the RX front end on and off
(PA13 and PA14 low):

| MHz | front end on | off | drop |
|---|---|---|---|
| 144.000 (6 x 24) | -77 | -132 | 55 dB |
| 168.000 (7 x 24) | -80 | -134 | 54 dB |
| 240.000 (10 x 24) | -96 | -142 | 46 dB |
| 156.000 (6 x 26) | -94 | -118 | 24 dB |
| 440.850 (PSK31, -18 dBm) | -18 | -72 | 54 dB |

The MCU clock's harmonics come in through the antenna path like a station, so switching the
front end off can't tell them apart; only the FD6818's own 26 MHz family stays (and a strong
station leaks through too, so a test would have to compare the drop). So the sweep marks a new
hit within 2.5 kHz of n x 24 MHz a spur at once (`ex_own_clock`), not after 60 s up, and lists it
with its level and the gull. The 26 MHz family is left alone: in our bands it only lands on
442.000, a channel.

## Not built yet

- The input-hop cue (the strongest cue) and the tone dwell (k6-explorer-next.md).
- Being on frequency before a repeater's 10-minute ID timer is due (remember each one's last ID
  time).
- The spectrum and waterfall screen. The rows are already computed (the sweep's `row`, the
  module's R lines); the image has 25.2 KB free for it.
- Try with transmit: this image has no VFO or PTT path, so Try only listens. That waits for
  EXPLORE in full Aura.
- A saved list that survives a power-off without Save: the list is RAM only, by Jeff's rule
  that flash is written only on Save or a confirmed call.
