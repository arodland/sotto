# K6 CW ID decoder

A RAM module for the Baofeng UV-K6 that listens on a repeater's channel, decodes its Morse
ID and (later) fills in the callsign of that repeater's entry in the explorer's repeater table.
This first step is a probe: it finds out which of the FD6818's detectors can follow CW keying,
and decodes what it hears so the answer comes with real numbers.

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

The bench test is to see which one follows 20 wpm keying cleanly.

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

## Bench test (hamlet, 440.850)

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

## Next

- Pick the detector from the bench result (and the tone detector's behaviour from the T lines).
- The listener module: one detector, two identical decodes before a callsign counts, then
  the repeater table's update rule (fill only an empty or CW-sourced call, never a user one;
  on a mismatch set the mismatch flag) through the explorer's `rt_find` / `rt_update_call`.
- Until then decodes go out as serial lines only; this module writes no flash.
