# K6 receive modes: the REG_47 AF select sweep

Jeff asked (2026-11-06) what the FD6818's receive modes do when REG_47 is changed. nicFW calls
one of them "USB", but on the bench it doesn't reject the opposite sideband. This page collects
what the sources say, and the module that sweeps every value on the radio.

## What the sources say

On Jeff's chip (ID 0x4819) REG_47 has the **BK4819 layout**, `0x6040 | (AF << 8)`, with the AF
select in bits 11:8. Aura's driver uses that layout (`AF_OUT_BASE = 0x6040`, RX audio = 0x0100),
and so does egzumer's UV-K5 firmware (`(6u << 12) | (AF << 8) | (1u << 6)`). Our `cwlisten`
writes 0x6140 to get receive audio. The FD6818 guide describes a 5-bit `afout_mode[4:0]` in
12:8 with only 0x10 mute, 0x11 RX AF, 0x15 alarm tone and 0x18 beep. That is the 0x6818's
numbering (see [k6-fd6818-audio.md](k6-fd6818-audio.md) and the shared register map). The guide
turns AM on with a separate bit, REG_04H bit 3 `amdem_en`, and REG_04 doesn't hold writes on
this chip.

| AF | egzumer `driver/bk4819.h` | Its comment | Aura (fd6818.rs) |
| --- | --- | --- | --- |
| 0 | MUTE | | mute |
| 1 | FM | FM | RX audio |
| 2 | ALAM | | RX alarm tone |
| 3 | BEEP | | beep |
| 4 | BASEBAND1 | RAW | |
| 5 | BASEBAND2 | USB | RX audio in USB/CW (`USB_AF_OUT_BIT` 0x0400 ORed onto 0x0100) |
| 6 | CTCO | strange LF audio .. maybe the CTCSS LF line ? | CTC/DCS test |
| 7 | AM | AM | RX audio in AM (`AM_AF_OUT_BIT` 0x0600) |
| 8 | FSKO | nothing | FSK test |
| 9 | UNKNOWN3 | BYP | |
| 10 | UNKNOWN4 | nothing at all | |
| 11 | UNKNOWN5 | distorted | |
| 12 | UNKNOWN6 | distorted | |
| 13 | UNKNOWN7 | interesting | |
| 14 | UNKNOWN8 | interesting | |
| 15 | UNKNOWN9 | not a lot | |

The original DualTachyon firmware names only 0, 1 (OPEN), 2, 3, 6, 7 and 8. The comments on
9-15 are the only notes we found anywhere, and nobody has measured those values. We couldn't
reach F4HWN's sources or a BK4819 datasheet.

The firmwares write more than REG_47 to change mode. egzumer's `RADIO_SetModulation` and Aura's
`apply_modulation` agree:

| Mode | REG_47 AF | REG_3D (IF) | REG_73 bit 4 (AFC off) | REG_14, REG_49 (AGC) | REG_48 (volume) |
| --- | --- | --- | --- | --- | --- |
| FM | 1 | 0x2AAB (8.46 kHz) | 0 | 0x0019, 0x2A38 | 0x8000 or 0xB000 \| vol << 4 \| 15 |
| AM | 7 | 0x2AAB | 1 | 0x0000, (50 << 7) \| 32 | 0xB3AA (Aura) |
| USB | 5 | **0x0000 (zero IF)** | 1 | 0x0019, 0x2A38 | as FM |

Aura's IF table also lists 0x4924 = 7.25 kHz. The AGC gain table is REG_10..13 = 0x007A, 0x027B,
0x037B, 0x03BE in both, with REG_7E bit 15 = fixed gain and 14:12 = the fixed index (3 = max).

**Why the "USB" is DSB.** USB mode moves the chip to zero IF and takes the baseband output. A
real mixer at zero IF folds the lower sideband onto the upper one, so both are heard, which is
what Jeff measured. Nothing in either firmware selects a sideband.

**What this means for GP0.** GP0 code 13 taps the receive IF before the chip's demodulator, so
REG_47 shouldn't change what PB1 sees. The chip's AF output reaches only the speaker. With the
normal 8.46 kHz IF (GP0 shows the carrier near 16.9 kHz), a signal above the carrier and one
below it land on opposite sides of the GP0 IF, so software on the K6 can tell the sidebands
apart. The chip's own USB mode can't. REG_3D = 0 would move that IF to 0 Hz, so a software SSB
receiver has to keep REG_3D at 0x2AAB.

## The sweep (`c/k6/mod/afsweep.c`, `tools/k6_afsweep.py`)

The module makes three passes, FM, AM and USB. Each sets the chip up as above, then waits for
the IC-7100's carrier and skips its 1 s lead tone. It then steps AF 0 to 15, with 0.3 s of mute
and 3.7 s of the value each, with the speaker amplifier on (PB2 high, Aura's
`set_speaker_switch`). Every 10 ms it reads RSSI, noise (REG_65), glitch (REG_63) and the AF
level (REG_6F[5:0]), and each step's line gives their min and max:

```
K6 MOD say s am 7 r-61 -57 n12 30 g0 3 a20 31
```

It puts back REG_47, 48, 3D, 73, 14, 49 and PB2 when it finishes, and returns the number of
passes done (-2 for a key, -6 if a pass's carrier never came). It doesn't transmit, and it is
1.4 KB of code at API level 2.

The sender plays a 65 s pass in each IC-7100 data mode (PKTFM, PKTAM, PKTUSB in hamlib). Each
pass is 1 s of 1 kHz, then 16 blocks of 4 s. Each block has 0.6 s of 1 kHz, a 400-2400 Hz sweep,
1 s of 700 + 1900 Hz, and 1 s of 300 Hz + 3 kHz. The sender unkeys for 4 s between passes and
ends with a CW ID.

```
python3 tools/k6_afsweep.py run --dry-run     # check the commands first
python3 tools/k6_afsweep.py run               # about 3.5 minutes
python3 tools/k6_afsweep.py log k6.log        # the step lines as a table
```

The serial cable mutes the K6's speaker. So run the sweep twice: once with the cable in for the
log, and once with it out and a phone recording the speaker. Steps then follow at 4 s intervals
from the first 1 kHz tone of each pass.

## Results

Not run yet.
