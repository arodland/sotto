# K6 CW ID decoder

A RAM module for the Baofeng UV-K6 that listens on a repeater's channel, decodes its Morse
ID and (later) fills in the callsign of that repeater's entry in the explorer's repeater table.
A probe module first found out on the air which of the FD6818's detectors can follow CW keying
(the tone detector); the listener module `cwlisten` is built on it.

## The problem

The K6's MCU can't hear the receive audio: the RF chip's AF output reaches no ADC pin. So the
keying has to come from the chip's own detectors, read over SPI. Jeff's chip reads ID 0x4819
and follows the BK4819 register map, not the numbering in the FD6818 guide (Aura's own driver
uses the BK4819 addresses). Three candidates:

| | Register (BK4819 map) | FD6818 guide | What it gives |
| --- | --- | --- | --- |
| A | REG_6F[5:0] | REG_87H (cmpd_db_out) | audio amplitude, 1 dB steps (on air: 0x23 with a carrier, 0x17 without) |
| B | REG_64 | (voice amplitude) | audio amplitude, linear |
| T | REG_24, REG_09, REG_0B, REG_0C | REG_94H, 1050 Hz recipe p. 32 | a tone detector: which of 16 programmed tones is present |

The bench test was to see which one follows 20 wpm keying cleanly (results below: T does).

## The decoder (`c/k6/cwid.[ch]`, MIT)

- **Level tracker** (`cw_lvl`): two levels, key down and key up, each following the samples on
  its side of the middle (tau 32 ms) while pulling the other slowly; keyed above the middle with
  hysteresis, but only once the two are 6 dB apart and 3 x the noise + 2 dB. The decision is
  made 16 ms (8 polls) late, against the levels as they are by then, so the first dot of an ID
  is judged with the key-down level it set up itself.
- **Key timing** (`cw_feed`): an 8 ms glitch filter with back-dated edges; marks and spaces kept
  in 4 ms units, one byte each; a transmission ends after 1.2 s of key up.
- **Speed and lag**: no setting. The marks split into dots and dashes (two-means), and since
  dot = u + e and dash = 3u + e, the dot length u and the detector's lag e both come out, so a
  detector that stretches marks by 20 ms still decodes. Letter and word gaps are a second
  two-means on the long spaces; a single cluster means letter gaps (Farnsworth spacing).
  Marks under 16 ms are noise and fold into the gap.
- **Callsigns** (`cw_call`): the first word shaped like a call (prefix with a letter, 1-3
  digits, 1-5 letter suffix), as heard up to 10 characters (`WA7ABC/R`, `VE9QRP/RPT`), with
  flags for /P, /M, /R (also a separate `R` or `RPT` after the call). `KH6/AG7EW/P` gives
  `AG7EW` with the portable flag.
- No division and no library calls (the M0 has no divide instruction and a module has no
  run-time library).

Host tests (`tests/test_k6_cwid.py`): 10-35 wpm, lag -10 to +20 ms, jitter, 2-5 ms polls: at
least 98 % decode exactly, the misses all at 28-35 wpm with 20 ms of lag (the gaps inside a
letter are then shorter than the glitch filter). Through a modelled level register (25-40 dB
of keying, 2-4 dB noise) the callsign comes out at 13-30 wpm; 8 dB of keying is marginal.
Noise alone produces junk transmissions but no callsign.

## The probe module (`c/k6/mod/cwprobe.c`, API level 2)

Its job is done (on-air results in the next section); it stays as the record of the run.

It needs a K6 image with module API level 2 (PR #59). It listens for up to 10 minutes on the
pager's channel (any key stops it), runs trackers on A and B, and feeds its one Morse decoder
from A for one transmission and from B for the next. It sets up the tone detector in
single-tone mode with 16 tones from 600 to 1350 Hz (coefficient 128 cos(2 pi f / 8463 Hz),
which fits Aura's DTMF table and the guide's 1050 Hz entry) and logs what it does, since how it
reports a keyed tone is unknown. It routes the receive audio to the speaker, so the ID is
audible. 3.9 KB of code (built with -Oz); its decoder state goes in the module store's spare
RAM (scratch), and it needs about 470 bytes of stack (690 if the scratch is too small). On the
612fb72 image the module call has 3,504 bytes of stack, so there is plenty.

Serial report (each line `K6 MOD say ...`):

```
cwprobe sp=0x... w=0x...                        stack pointer, where the decoder state is
cwprobe rx=440850000 th=24 idle=1 s=600         frequency, tone detector settings, listen time
A 20wpm lag=8 n=61 lo=12 hi=40 dv=1             detector, speed, mark lag (ms), levels (dB)
A AG7EW SOTTO TEST                              the text
A d +60 -60 +184 -180 ...                       marks (+) and spaces (-) in ms
T 2016 b=1400                                   tone detector: REG_0B[12:8] changed (ms, value)
T 2020 i=0800                                   tone detector interrupt (REG_02 cause)
R 6f 0008..002b                                 each register's range over the run
cwprobe end key msgs=2
```

## Probe bench test (hamlet, 440.850, as run)

1. A K6 image with level 2 (the explorer thread's run flashes it).
2. Send the module and, right after it, an ID or two (on a machine without clang, use the CI
   artifact's `mod-cwprobe.bin` with `--api-level 2` in place of the source):

   ```sh
   python -m modem.k6 mod probe.wav c/k6/mod/cwprobe.c --key-file ~/.config/sotto/k6-mod.key --phy F --payload XL
   python -m modem.k6 cw id.wav "AG7EW SOTTO TEST" --wpm 20 --tone 800 --repeat 2 --gap 4
   ```

   Play probe.wav, then id.wav, keyed as usual (it identifies the station too). Two IDs give
   one decode on A and one on B. Try 13 and 25 wpm, and tones 600 and 1200 Hz.
3. Stop it with any key on the K6, or wait for the 10 minutes.

## Probe results (hamlet, 2026-11-06 05:09-05:19 UTC)

Image 6473ca7 (level 2), module call stack 3,512 B, scratch 204 B (too small for the probe's
216 B, so the decoder ran on the stack; 864 B were left). IDs from the IC-7100: 20 wpm at
800 Hz, 13 wpm at 600 Hz, 25 wpm at 1200 Hz, each sent twice with a 4 s gap.

- **A, REG_6F: too slow.** Only 7 dB between key up and key down (26 and 33 dB), and the
  marks and spaces it gave for the 20 wpm IDs sit on a lattice of about 120 ms (marks 104-128
  and 340-356 ms, spaces 132, 240, 360, 472, 600, 724, 852 ms) where a dot is 60 ms: the
  register updates (or averages) about every 120 ms. The decoder made "3 wpm", text `#`, and
  took both IDs and their gap as one transmission.
- **B, REG_64: never moved** (0x000a throughout). It is the TX (mic) amplitude, as UV-K5
  firmware uses it for VOX. The probe's turn-taking then gave every later transmission to B,
  so it reported only one.
- **T, the tone detector: follows the keying.** REG_0B read 0xC006 idle and 0xD006 with the
  600 Hz tone: bit 12 is set while a programmed tone is present and [11:8] says which (tone 0
  = 600 Hz; the module upload's FFSK gave tone 15). The 13 wpm ID's first letters, from the
  event log (times are ms mod 65536, so 8639 is 74.2 s after the start):

  | | on | off | on | gap | on | off | on |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | sent (13 wpm) | 92 | 92 | 277 | 277 | 277 | 92 | 277 |
  | REG_0B bit 12 | 102 | 82 | 288 | 266 | 286 | 83 | 286 |

  That is A (`.-`) and the start of G (`--.`): every mark about 10 ms long, every space 10 ms
  short, which the decoder takes out as lag. Each tone-on also raised the interrupt (REG_02
  cause 0x0800). The event log (12 entries) overflowed during the 20 and 25 wpm IDs, so those
  aren't confirmed edge by edge, but at 10 ms resolution a 48 ms dot (25 wpm) is easy.

## The listener module (`c/k6/mod/cwlisten.c`, API level 2)

The tone detector set up as the probe set it (16 tones, 600 to 1350 Hz in 50 Hz steps,
threshold 24), REG_0B bit 12 fed straight to the decoder every 2 ms, and a count of which tone
was on during the marks. Per transmission, on the serial report (each line `K6 MOD say ...`):

```
CWID listen rx=440850000 th=24 s=1500                   start: frequency, threshold, time limit
CWID 20wpm lag=10 tone=800 q=98 n=61                    speed, lag (ms), tone, % of marks on it
CWID text AG7EW SOTTO TEST
CWID call=AG7EW fl=0 seen=1                             callsign, flags (1 /P, 2 /M, 4 /R), count
CWID d +60 -60 +184 ...                                 marks and spaces, ms
CWID ok AG7EW fl=0 rx=440850000 tone=800 wpm=20         the same call twice in a row
CWID end ok msgs=2 short=7                              why it stopped (ok, key, time); IDs
                                                        decoded; short bursts with no call
```

It stops at the ok line, on any key, or after 25 minutes (two IDs 10 minutes apart and some),
and returns 1 if it confirmed a call. 3.95 KB (-Oz), 360 B of stack with its 210 B of state in
scratch, 576 B without (the K6 gives it about 167 B of scratch, so on the radio it's the
stack, of which there are 3.5 KB). Host tests: 13, 20 and 25 wpm on tones 0, 4 and 12, after
upload-tail bursts of 1-7 marks on tone 15 (only counted); two different calls confirm nothing.

To run it (a machine without clang sends the CI artifact's `mod-cwlisten.bin` with
`--api-level 2`):

```sh
python -m modem.k6 mod listen.wav c/k6/mod/cwlisten.c --key-file ~/.config/sotto/k6-mod.key --phy F --payload XL
python -m modem.k6 cw id.wav "AG7EW SOTTO TEST" --wpm 20 --tone 800 --repeat 2 --gap 4
```

### On air (hamlet, 2026-11-06 05:30-05:35 UTC, image 6473ca7)

Three runs on 440.850 MHz, with the IC-7100 at RFPOWER 0. Each run sent a freshly built
module (with its own serial), then one ID twice with a 4 s gap (`--repeat 2 --gap 4`),
keyed 3 s after the upload's carrier dropped. The module call had 3,512 B of stack and 164 B
of scratch, so the state ran on the stack, and 864 B of stack were left at the end of every
run.

| Run | ID | ok line | Lag | ok after PTT off | Run to ret | msgs |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 20 wpm, 800 Hz | `CWID ok AG7EW fl=0 rx=440850000 tone=800 wpm=20` | 12 ms | 0.48 s | 48.5 s | 9 |
| 2 | 13 wpm, 600 Hz | `CWID ok AG7EW fl=0 rx=440850000 tone=600 wpm=13` | 12 ms | 0.41 s | 53.2 s | 5 |
| 3 | 25 wpm, 1200 Hz | `CWID ok AG7EW fl=0 rx=440850000 tone=1200 wpm=25` | 8 ms | 0.52 s | 45.2 s | 8 |

- All 6 copies decoded exactly as `AG7EW SOTTO TEST`, with n=61, the right tone, and every
  mark on it (q=100).
- Every run confirmed on the second copy and returned 1. No ID had to be sent again.

Marks and spaces as reported, in ms (4 ms units). The heard values are min-max over both
copies; each copy has 14 dots, 17 dashes, 17 element spaces, 11 letter spaces and 2 word
spaces:

| | Dot | Dash | Element space | Letter space | Word space |
| --- | --- | --- | --- | --- | --- |
| 20 wpm sent | 60 | 180 | 60 | 180 | 420 |
| 20 wpm heard | 68-72 | 188-192 | 48-52 | 168-172 | 408-412 |
| 13 wpm sent | 92 | 277 | 92 | 277 | 646 |
| 13 wpm heard | 100-104 | 284-288 | 80-88 | 264-268 | 636 |
| 25 wpm sent | 48 | 144 | 48 | 144 | 336 |
| 25 wpm heard | 52-56 | 148-156 | 40-44 | 136-140 | 328 |

Every mark is 8-12 ms long and every space as much short, as in the probe's event log. That is
the detector's lag, which the decoder measures and takes out. Within each kind the spread is
one or two 4 ms units, so there's plenty of margin at these speeds.

**False decodes during the upload.** A module starts as soon as its last chunk is in, while
the sender is still keyed and repeating chunks. Over that FFSK the listener reported short
transmissions on tone 15 (1350 Hz, the top tone): 7, 3 and 6 per run, of 1-7 marks each (n
1-13). They read as `E`, `EE E`, `NE`, `TE E E`, `I E EE E` at "23-60 wpm", with q 77-100, and
they are the extra msgs in the table (msgs = false + 2). None looked like a callsign, none
came after the carrier dropped, and none came from the quiet channel between IDs.

Since then, a transmission of under 11 marks (n < 21) with no callsign in it is only counted,
on the end line (`short=`). The shortest call, `K1E`, is 9 marks, and it still gets through
because it has a call. This filter is tested on the host, not on the air yet.

**Upload.** Runs 1 and 3 got all 33 chunks on the first pass. In run 2, chunk 5 was bad
(`r=-1 xfer=75 chunk=5 have=5/33`). The sender's second pass filled it (five `r=2` repeats,
then `r=3` on chunk 5), so no `--only` resend was needed.

**Bench notes.**

- This image boots with frames=L, so a run needs `ctl --set frames=XL` (sent as L) and an
  `--action none` confirm (sent as XL) before the XL module packets.
- Send a station ID just before the first module, so the ID keeper doesn't key its own ID
  during a run.
- The raw logs are on hamlet: `~/k6-logs/cwprobe-20261106T0506.log` and
  `cwlisten-20261106T0529.log`.

Unknowns for real repeaters: an ID tone outside 600-1350 Hz, an ID sent over voice (the
detector may not lock), and whether 50 Hz steps are finer than the detector can tell apart
(the 'tone' field and q will show it).

## Next

- What else the FD6818 shows the MCU (meters, the I2S lead): [k6-fd6818-audio.md](k6-fd6818-audio.md). Ideas from this work are in the insights list in [k6-modules.md](k6-modules.md#insights-a-living-list).

- `cwlisten` on a real repeater's ID.
- The table update: the explorer's `rt_find` / `rt_update_call` once they exist (fill only an
  empty or CW-sourced call, never a user one; on a mismatch set the mismatch flag; byte 51 gets
  the tone and speed). Until then the ok line is the result, and this module writes no flash.
