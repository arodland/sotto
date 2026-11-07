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

The screens use the 5x8 font (Jeff, 2026-11-07): 8 rows of 25 characters, with 6 candidates on
the list at a time.

```text
EX ALL 11 2.3s UP          band set, candidates, one pass, UP while something is keyed
>R 146.940 -0.6  -80       grade, MHz, offset (S simplex), loudest level
 r 443.100 +5.0  -85
 ? 440.850 +5.0  -22
 s 146.520   S   -70
n4 sc8 W7ABC/R             the selected row: overs, score, call (or ids)
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
