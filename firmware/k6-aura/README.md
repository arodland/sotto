# VOICE MSG: sotto voice pager firmware for the Baofeng UV-K6

Aura, the open Rust firmware for the Baofeng UV-K6 / K61 family
([sophiel-meow/bf-k61-firmware-aura](https://github.com/sophiel-meow/bf-k61-firmware-aura)), with a
**VOICE MSG** app added. The radio receives a sotto voice message as FSK packets through its own
radio chip's modem, keeps it in RAM only, and plays it on the speaker through the MCU's PWM pin.
That pin is the one the stock firmware uses for its voice prompts. There are no hardware changes,
and nothing is written to flash per message. The design and the numbers are in
[`docs/k6-receiver.md`](../../docs/k6-receiver.md). The packet format is `k6-fsk-packet-spec.md`
(version 1), and its reference packer is [`c/k6/k6fsk_ref.py`](../../c/k6/k6fsk_ref.py).

- `aura-pager.patch`: the change to Aura at commit `edf3b98` (2026-11-02). It is GPL-3.0, like Aura.
  It adds `src/app/pager.rs`, `src/hal/pwm_audio.rs` and `src/ui/pager.rs`, and wires them into the
  launcher, keys, screen and main loop. It also adds two raw-register helpers to the FD6818 driver
  and makes `build.rs` link `lib/libsotto_k6.a`. The cargo feature `pager-only` makes VOICE MSG the
  whole firmware.
- `build.sh` clones Aura at that commit, applies the patch, and builds sotto's C library
  (`tools/k6_build.sh`, MIT). It then builds stock Aura, Aura with VOICE MSG added, and the
  pager-only image, and reports each one's flash and RAM use. It writes `out/aura-pager-only.bin`
  only if it fits.
- The GitHub Actions job *K6 pager firmware* runs the same build. Each run keeps the `.bin`, the
  ELF and the size reports as a downloadable artifact.

**Why a pager-only image.** Stock Aura uses 115,208 of the 122,880 bytes of application flash,
and the pager needs 66 KB (30 KB of it is the codebook). All of Aura with VOICE MSG comes out 58 KB
too big. The pager-only image is 102,228 bytes (20 KB free) with 9,144 bytes of static RAM. It keeps Aura's drivers, its saved settings and VFO, a frequency entry, and the VOICE MSG
app, and leaves out the rest: the standby screen and menus, scanning, FM broadcast, SSTV, APRS,
satellites, the spectrum and CHIRP programming. To use the radio normally, flash stock Aura back
(same UPDATE-mode steps). Each image switch is one erase of the MCU's flash. The SPI flash, which
holds the settings, isn't touched by the switch.

**Status, 2026-11-03.** The sotto side is tested on this machine and in the Cortex-M0 emulator.
The Rust side builds in CI, but nobody has run it on a radio yet. Treat the first flash as a bring-up test, with the calibration steps
below.

## Before you flash: what can't be undone

- **Flashing Aura replaces the stock firmware for good.** Baofeng never released the stock image,
  and the bootloader can't read it back out. If your K6 already runs Aura, this is just an Aura
  update.
- **Only the Baofeng K6 / K61 with the FD6818 RF chip works.** That means CHIPID 0x4819. Boards with
  the FD6818B (0x4829) need Aura's experimental `fd6818b` branch, and the pager isn't ported to it.
  The pager screen says `RF chip not FD6818` and won't listen if the chip is wrong. The Quansheng
  UV-K5/K6 is a different radio and won't take this image.
- **Flash writes.** Installing writes the MCU's internal flash once. On Aura's first boot, the
  `FORMAT FLASH?` prompt formats part of the 2 MB SPI flash once. That part holds Aura's settings,
  VFO, contacts, satellites, FM, logo and SSTV image, but not the calibration. After that the pager
  never writes flash. Aura itself still saves settings and channel state to the SPI flash
  (wear-levelled) when you change them or power off, as it always does.

## Flashing and backup

You need a Quansheng-K5-style USB-to-TTL programming cable (the Kenwood 2-pin plug), Python 3 with
`pyserial`, and Aura's `tools/` folder. After `build.sh` that folder is at `build/aura/tools/`, or you
can clone Aura.

1. **Get `aura-pager-only.bin`.** Download it from the *K6 pager firmware* run's artifacts on the pull
   request, or run `sh firmware/k6-aura/build.sh`. That needs rustup, clang and lld, and llvm-tools.
2. **Enter UPDATE mode.** With the radio off, hold **Side Key 1 and Side Key 2** and turn it on. The
   screen shows `UPDATE`.
3. **Flash it:** `python tools/flash.py aura-pager-only.bin /dev/ttyUSB0` (on Windows, the COM port).
   On success it prints `succeed!` and the radio reboots. If it doesn't reboot, nothing was written
   (a known Aura bug): power off and repeat from step 2. Pulling the battery mid-flash is safe; just
   flash again.
4. **Back up the SPI flash before formatting.** On the first boot the radio shows `FORMAT FLASH?`.
   Leave it on that screen, plug in the cable, and run
   `python tools/dump_aura_spiflash.py /dev/ttyUSB0 k6-spiflash-backup.bin`. It reads all 2 MB, the
   factory calibration included, in a few minutes. Run it again if it stops partway. Keep the file.
   You can also save the bootloader with `python tools/dump_bootloader_aura.py`. Then press **MENU**
   to format.
5. **Check the chip:** Settings → SYSTEM → CHIPID must read `0x4819`.

## Using VOICE MSG

1. Flash `aura-pager-only.bin` and power on. The radio goes straight to VOICE MSG and listens,
   with the speaker muted, on 440.850 MHz (`DEFAULT_FREQ_HZ` in `pager.rs`: WWARA's packet
   simplex sub-band, 440.800-440.900; change it there for another region). The pager-only image
   writes nothing to flash, not even at power-off.
2. **Change the frequency** with **#**, then six digits in kHz, the same way as Aura's VFO entry.
   For example, `# 4 4 0 8 5 0` gives 440.850 MHz, and `# 1 4 6 5 2` then MENU gives 146.520 MHz
   (MENU fills the rest with zeros). Below 100 MHz, start with a 0. EXIT deletes a digit, or
   cancels when nothing has been typed. The frequency is kept in RAM only, so at power-on the
   radio is back on the default. The bandwidth (wide or narrow) and offset are Aura's saved
   settings. FM wide is the usual setting.
3. The message plays by itself once every frame has arrived, or 3 s after the last packet if some
   are missing. Missing 20 ms frames are concealed. **MENU** plays it again, and **EXIT** stops a
   playback.

(In a build of all of Aura with VOICE MSG, if a later Aura ever has room: MENU, choose **VOICE MSG**,
MENU again; EXIT leaves the app.)

| Key | Does |
|---|---|
| MENU | play the message (again) |
| EXIT | stop playback / leave |
| UP / DOWN | volume, 3 dB steps on top of the automatic level (-9 to +12 dB) |
| 1 | PHY: F (FFSK 1200/1800 Hz; 1200/2400 Hz at 2400 baud; the default) or D (direct 2-FSK) |
| 2 | frames: L (64-byte packets, 8 frames, the default) or XL (128 bytes, 20 frames; screen `X`). Profile S is gone |
| 3 | the RF chip's RX invert bit (REG_59<10>), by hand only |
| 4 | pre-emphasis on/off (undoes the PWM filter's -10 dB at 3 kHz) |
| 6 | raw view: the last packet's first 16 bytes in hex, its sync and length, and its bit errors against the test message (below) |
| 5 | 1200 / 2400 baud, D or F (F at 2400: REG_58 0x73E9, FFSK 1200/2400 Hz; untested on air) |
| 7 | how the packet length goes into REG_5D: bytes - 1 or words - 1. (Images up to 647a60e also had two stream modes, 0xFF00 and 0xFFE0; they never received on air and were compiled out to make room for REG_43 in the control-packet registers.) |
| 8 | Re-arm delay after each packet: 40, 50, 30, 60, 20, 0, 80 ms after its end, then a per-packet sweep (`CFG rearm=`; -1 = sweep). FIFO reads are always on a timer now |
| 9 | auto-play on/off (on by default; MENU still plays) |
| 0 | The next receive variant (`CFG f2400= reg58= reg2b=`), 11 of them, var=0 to 10: (REG_58 at F/2400, bits ORed into REG_2B) = (3fe1, 0), (3fe9, 0), (3fe9, +0100), (3fe9, +0700), (3ce9, 0), (3de9, 0), (3ee9, 0), (73e9, 0), (3fe1, +0100), (3f09, 0), (3f01, 0). REG_58 per the UV-K5 firmware notes: bits 12:10 RX tone mode (7 = 1200/1800, 4 = 1200/2400, 0 = direct), 9:8 RX gain, 3:1 RX bandwidth (0 = FSK 1.2K, 1 = FFSK 1200/1800, 4 = FSK 2.4K and FFSK 1200/2400). REG_2B: +0100 RX de-emphasis off, +0200 the 3 kHz LPF off, +0400 the 300 Hz HPF off. The REG_58 value applies only at F/2400, so the variants with the 2.4K bandwidth (3fe9 etc., deaf at 1200) can be tried by sending `--set baud=2400 var=N` in one control packet: the revert then lands back at 1200. The REG_2B bits apply to D and F at any baud. Until this image variant 0 was 73e9, which never synced, nor did its relatives (7329, 73e3, 7fe9, 70e9, 63e9) or D/2400; 3fe1 syncs at 2400 with 1200/1800 tones but the bit clock slips |
| * | clear the message |
| # | type a frequency (kHz, six digits; MENU sets it early, EXIT deletes) |
| PTT | calibration transmit: test packet 0, twice in D and then twice in F, at key 5's baud rate, on the VFO's TX frequency and power |

The screen:

```
VOICE MSG  440.85000     frequency
D L 1200 i-x-s+ e+ +0    PHY, frames, baud, chip invert, soft invert, byte swap, emphasis, dB
ID 42  640/640  80/80    message id, frames received/stored, packets received/total
SYNC P CRC ok A7 TO 0    last sync polarity, CRC, magic byte (A7 = right), FIFO timeouts
DEC 11.8/17.2ms 2930     after a playback: decode ms per 20 ms frame, mean/worst; free RAM bytes
MENU play  PTT test
```

A message holds up to 640 frames (12.8 s). `STORE_BYTES` in `src/app/pager.rs` sets that, at 6.2
bytes per frame. Raise it if the free-RAM figure leaves room.

## Control packets over the air (spec section 13)

The PC can change the receive settings without a key press: `python -m modem.k6 ctl` makes a
control packet, an ordinary L or XL payload (whitened, same preamble and sync) filled with
copies of one 8-byte record (`A8`, seq, mask, values, variant | volume << 4, action, CRC-16).
The K6 takes the first copy whose CRC checks, in any byte order or polarity, so the early copies
get through even on D/1200, where errors build up later in a packet.

```
python -m modem.k6 ctl ctl.wav --phy F --set frames=XL            # F/L -> F/XL
python -m modem.k6 ctl ok.wav --phy F --payload XL                # confirm (no change)
python -m modem.k6 ctl ctl.wav --phy D --also F --set phy=F       # PHY unknown: D, then F
python -m modem.k6 ctl play.wav --phy F --action play             # play the stored message
```

Settings: `phy=F|D frames=L|XL baud=1200|2400 auto=0|1 emph=0|1 var=N vol=0..7 idlekeep=0|1`; actions
`play`, `clear`, `report`, `regs`. The K6 prints `K6 CTL seq=… mask=… changed=…` and a CFG line with
`why=ctl`; a record whose seq it just applied only confirms (`K6 CTL seq=… repeat`).

**It can't strand the radio.** After a change of PHY, profile, baud, variant or idlekeep, the K6 goes back
to the settings before it unless a good packet (voice or control) arrives within 60 s, and says
`K6 CTL revert` with a CFG line `why=revert`. So follow every change with a confirmation in the
new settings. A key press wins over all of it. Control packets change nothing else: not the
frequency, and nothing that transmits. Anyone on the channel could send one, but it only
changes this receiver's settings, and only until the next key press or power cycle.

**`idlekeep` (on at power-on from the build after 0438507).** While idle, the chip gives an
RX-done with no sync every 40 to 53 ms in the lead-in's 0x55, and the K6 used to re-arm on each
one. So the last re-arm always came 40 to 53 ms before the first packet's sync (`gap=`), too
little preamble to lock, and the first packet of a burst failed about half the time (839924e,
2026-11-04). With `idlekeep=1` the K6 leaves the chip hunting instead, as it already does for
the late RX-done inside a burst. On air (0438507, 2026-11-04, no throwaway packet, 20 bursts
each): first packet good 20/20 and 60/60 packets with idlekeep=1, against 7/20 and 46/60 with 0;
VE9QRP as L 500/500 frames. `ctl --set idlekeep=0` turns it off (it reverts after 60 s without a
good packet). The CFG line shows `idlekeep=` and `idlerd=`, the count of idle no-sync RX-dones since
the last CFG line. The softmodem's `--prime` (a throwaway first packet, on by default) is the
workaround for images before this one; it costs one packet and is harmless after it.

**Register writes (`--reg`, experiment, not yet on air).** For settings the register list
doesn't settle, such as 2400 baud receive, a control packet can write one FD6818 register:
`ctl reg.wav --phy F --reg 58=73e9`. Allowed: `2b` (AF filters), `43` (RX bandwidth, the
wide/narrow setting Aura saved for the VFO; written back on every arm, from the image after
647a60e), `58` (FSK mode), `59` (low byte only: preamble and sync lengths), `5c` (CRC and
scrambler), `72` (baud), and from the image with AFC writes `44` (gain after the FM demodulator,
bits 12:11), `73` (AFC: bits 13:11 range from 2.2 kHz down to 188 Hz, doubled in wide; bit 4
turns AFC off) and `7e` (bit 6 bypasses the RX DC filter). From the image after that, also `96`
(receive discriminator gain, bits 13:8), `9b` (the FD6818 guide's RX filter bypasses: 9 HPF, 8
LPF1, 7 LPF2, 6 de-emphasis; the guide is for chip ID 0x6818, ours reads 0x4819, so 96 and 9b
may do nothing here) and `5e` (FIFO thresholds). Like 43, 44 to 9b are read once and written
back on every arm, so a revert undoes them. **A write to 5e also switches the receiver from
timed FIFO reads to interrupt reads:** on each FIFO-almost-full interrupt it reads the RX
threshold's worth of words, taken from bits 2:0 when they aren't 0, else from bits 9:3 (which
field the chip uses is the open question; the image's own 0x0204 has 4 and 64). The `ev` field
of each PKT line shows when those interrupts came. The revert, a key press or `--reg 0=0`
brings the timed reads back.

On air (2026-11-05, e0754fd) the RX threshold is bits 2:0, in words: with `5e=0202` the
interrupt came every 26-27 ms at 1200 baud (two words) and the words were right. It is an
edge, though: with N or more words still waiting after a read it doesn't come again (5e=0201
fired twice a packet), and reading an empty FIFO repeats its last word. So the interrupt reads
now drop an RX-done that comes with no sync or too soon after one (a stale one had read 32
words of repeats over the next packet's start), read what is left (at most the 8-word FIFO) at
RX-done, and after N+1 word times without an interrupt take the words due since the last read
(pseudo event `1@ms+n` in `ev`; one word at a time fell behind). They stay in the burst loop
between packets like the timed reads. When `ev` is full its last entry shows the latest event.

**Interrupt order.** The UV-K5 BK4819 drivers write REG_02 = 0 first and then read REG_02:
the write latches the pending causes for the read. This firmware has always read first. If the
chip latches as those drivers assume, every cause it saw was the previous interrupt's, one event
late. That fits the stale RX-done at the start of each burst, the sync seen about two words late,
and the almost-full interrupt that stops coming once the FIFO is no longer emptied after the
sync (25d4a58, 5e=0202: one interrupt per packet, the rest by time). So the interrupt reads
(any `5e` write) now clear first and then read; the timed reads keep the old order, which their
lead words are tuned to. If clear-first hears nothing, the 60 s revert brings the old order back. On air (0cb0ff4,
2026-11-05) clear-first was right: `5e=0203` gave 22 of 22 packets at F/1200, an interrupt every
3 words; `5e=0202` 19 of 24 (16/16 on the L message, some packets split at the end). Field meanings are from the FD6818
programming guide. The K6 refuses anything else. It holds up to 4 writes, applies them after
its own setup on every re-arm, and prints `K6 REGS seq=… 43=… 44=… 73=… 7e=… 96=… 9b=… 5e=… 2b=… 58=… 59=… 5c=… 5d=… 72=…
poke=N` as read back from the chip (43 from the image after 647a60e). Like a PHY
change, the writes are undone after 60 s without a good packet, and by a key press. `--reg 0=0`
drops them all, and `--action regs` only prints the registers. On air (2026-11-04) the K6
didn't sync on replays of its own F/2400 or D/2400 bursts, which decode on the PC, so the 2400
fault is in the receive setup.

**What the register sweep found (0438507, 2026-11-04 evening).** Baseline at F/1200: `2b=c000
58=3fe3 59=10f8 5c=5625 5d=3f00 72=3065`. The only setting that syncs at 2400 baud is REG_58
**3fe1** (the 1200/1800 tone mode with bits 3:1 = 000; 1200 uses 001) with 1200/1800 tones
(`--tones 1200,1800`): every burst syncs, but no packet passed its CRC. Against the packet sent,
the data is right but the chip's bit clock wanders a bit either way every 100 to 150 bits
(sending 0.5 to 1.5% slower or faster moves the drift, doesn't stop it), and some packets lose
a whole FIFO word at about bit 128 (the first timed read is late at 2400). Deaf at 2400: 3fe3,
3fe5, 3fe7, 3fed, 3fef (3feb 1 sync in 5); D mode 00e9, 00eb, 00ed. Deaf even at 1200: 73e9,
73e1, 33e1, 63e1, 3fe9, D 00e3, 00e5, 00e7, 00ef. 7fe1 keeps F/1200 but hears no 1200/2400
tones at either baud. So the 1200/2400 tone mode never synced. Write 58 at 1200 first, then
`--set baud=2400`, so the firmware paces its reads for 2400.

**DC cancellation off by default.** REG_2B bit 14 is the AF DC canceller after the FM
demodulator. The firmware clears it for every PHY and baud. With it on, direct FSK at 1200
passed 7 of 28 packets, and the last 16 bytes of each failure were corrupt. With it off it
passed 31 of 31 (`2b=8000`, `8400`, `0400`, and `8000` with interrupt reads). FFSK at 1200 stayed
clean (15 of 15). `--reg 2b=c000` turns it back on for 60 s.

**Bootloader dump (action 6).** `modem.k6 ctl --action boot` makes the K6 print its stock
bootloader (flash 0x0800_0000, 8 KB) on the serial report as `K6 BOOT off=` lines, about 2 s;
`python -m modem.k6 bootdump LOG out.bin` turns a log of them back into the image. It is read
only, and it is for finding how the bootloader enters UPDATE mode, so that a later control
action can reboot into it without the side keys. The image is Baofeng's code: keep it on your
own machine, never commit or share it.

**Flashing without the side keys (action 7, images from db796ee).** The dump showed that after
every reset the stock bootloader listens on the cable for about 200 ms. One handshake frame
(`AA 42 00 00 00` + CRC-16/XMODEM + `EF`) gets an answer and keeps it in UPDATE mode, as the
side keys do. There is no RAM flag, and the app check is only a valid stack pointer at
0x0800_2000. `modem.k6 ctl --action reset` makes the K6 print `K6 RESET seq=N`, wait 30 ms and
reset. `tools/k6_update.py IMAGE` waits for that line on the cable, sends the handshake every
18 ms for 1.5 s, and on the answer runs Aura's `tools/flash.py`. It writes to the cable only
after the reset line. With `--manual`, start with the K6 off and power it on (no keys).
`--trigger "CMD"` runs the command that transmits the reset packet once the port is open.
Without an answer, the window passes and the pager boots again, so a missed window costs
nothing. Stop any serial listener first, because the tool needs the port.

**sotto 1200 image (`out/aura-pager-lr.bin`).** build.sh also links the pager with the sotto
1200 library (`K6_CODEC=1200 sh tools/k6_build.sh`, `-DSP_LR`): it plays magic 0xA9 messages
(`modem.k6 send --codec 1200`), 40 ms per stored slot, so the 640-slot store holds 25.6 s and an
XL packet carries 800 ms of speech (640 ms on air at F/1800 with 12-byte preambles, 0.8x real
time). Both images take both kinds of packet (sync, CRC, bit-error tests); each plays only its own
codec. M0 emulator, decode plus PWM per 20 ms: 53-61 MHz mean, 75-78 worst of 96 (three clips),
the same as v3.7; image 119,236 B, 3.6 KB free (sotto_pager.c now at -Os, 0.1 MHz cost).

**Other bit rates.** REG_72 sets the chip's bit rate (rate x 10.3244: 0x3065 = 1200,
0x4898 = 1800, 0x50A9 = 2000, 0x60CA = 2400), and the firmware paces its reads and waits from
the REG_72 value in force, so `--reg 72=4898` runs it at 1800. On air (2026-11-05, e904cb0,
F with 1200/1800 tones, `2b=8000 5e=0203 58=3f01`, `modem.k6 --baud 1800`), 1800 got 12 of 12
packets and 2000 got 10 of 12, every decoded packet with 0 bit errors; 2400 slips a bit.

**F runs at 1800 by default (images after 0dad5c6).** F boots with that recipe: REG_58 3f01,
REG_72 4898, interrupt reads with REG_5E 0203, DC cancellation off. That makes control packets
and messages 1.5 times faster. `modem.k6` follows it: `--baud` defaults to 1800 for `--phy F` and
1200 for D. A control packet with `baud=1200` means "the PHY's own rate", so F/1800. Key 1
(D/1200) or `--reg 72=3065` (F/1200 with the old 3fe3 recipe, for 60 s) gets 1200 back, and key 5
or `baud=2400` still gives 2400. The revert lands on F/1800.

## First tests (spec section 8)

1. **K6 transmits, IC-7100 records.** In VOICE MSG, press PTT. The radio keys up for about 3 s and
   sends test packet 0 twice in profile D and twice in profile F. Record the IC-7100's receive
   audio; the softmodem decodes it to settle polarity, bit order, FIFO byte order and deviation.
   Jeff's first burst (2026-11-04) decoded with no bit errors in D and F. It showed that the FD6818
   sends each 16-bit FIFO word low byte first. The firmware now packs TX that way and swaps on RX
   by default (`s+`), so both directions are in spec byte order on air.
   `TX not allowed here` means Aura's band lock refused that frequency or the VFO isn't FM.
2. **IC-7100 transmits test packets, K6 receives.** Watch the SYNC line.
   - `SYNC -` that never changes, with no `bad` count either: no sync found. Check the level and
     deviation, or press 1 to try profile D. Lower-case `p`/`n` mean the sync interrupt didn't fire
     but the chip's polarity flags were set when the packet ended.
   - Jeff's first IC-7100 → K6 test in F (2026-11-04, old build) gave magic byte `58`, which is
     `A7` with every bit inverted. The FD6818's FFSK receive seems to map the two tones the opposite
     way from its FFSK transmit. This build finds that by itself and shows `x+`.
   - `SYNC N`: the chip found the sync with inverted polarity. Until the first good packet, the
     radio tries each packet four ways: as read, bytes swapped, bits inverted, and both. The
     check is the magic byte, or the CRC for profile S. It keeps whichever works: `x+` means
     inverted in software, and `s+`/`s-` is the byte order (`s+`, low byte first, is the measured
     default). The chip's own invert bit (`i`) only changes with key 3.
   - `CRC ok A7`: the packet arrived intact.
   - `CRC BAD`: none of the four ways passed, so there are bit errors or a framing problem.
     Press 6 for the raw view. A rising `TO` count means the chip's FIFO interrupts didn't
     arrive as expected. Try 2 for the 16-byte frames.
   - **Bit errors.** While test packets come in (`python -m modem.k6 test`, either profile), the
     raw view counts each packet's bit errors against the spec's test message:

     ```
     R0 58A31D9AEDC33324     first 16 bytes as read (after the s swap, before x)
     R8 FB58E17D78974628
     SYNC N A7 64 av3.5      sync, magic byte, bytes read, mean errors per packet
     BE   3 s+x+ 00010359    last packet: errors, the reading that fits, errors per eighth
     ```

     `BE` is the bit-error count of the last packet (of 512 in L, 128 in S), against whichever
     of the test message's packets and whichever of the four readings fits it best. `s` and
     `x` show that reading, which is how the radio should be set. The eight characters count
     the errors in each eighth of the packet: 0-9, then a-z for 10-35, then `+`. Random data
     reads about 32 per eighth in L (`w`). `av` is the mean over all packets since the last
     link-key change or `*`, so it compares transmit speeds or levels. Errors that grow
     towards the end of the packet point at a baud-rate mismatch, and a step to about half
     at one place points at a lost or extra bit. A real message (not the test message) reads
     about half its bits wrong here, as it should.
3. **A test message, then a real sotto clip.** The PC encodes with `c/sotto_enc` (v3.7 frames), and
   the softmodem sends them (`python -m modem.k6 tx`, see `docs/k6-pager.md`). After it plays, note the DEC line. The
   emulator predicts about 11-13 ms mean and 15-18 ms worst per 20 ms frame. Note the free RAM too:
   it should stay well above zero.

## Serial test harness

The pager image reports each event on the programming cable as one line of text: every
packet with its raw bytes, each setting change, each playback, and each calibration transmit.
The format is 115200 8N1, radio to PC only. The radio never reads the cable, because the
cable's other wire is the K6's PTT line. So leave the cable plugged in during tests, and don't
send anything to that port.

`python -m modem.k6_harness` on the PC reads it. In the examples below, the K6 cable is
`/dev/ttyUSB2` and the IC-7100's CI-V port is `/dev/ttyUSB0`.

```
python -m modem.k6_harness listen --k6 /dev/ttyUSB2 --log k6.txt
python -m modem.k6_harness sweep --k6 /dev/ttyUSB2 --port /dev/ttyUSB0 --phy F --player paplay \
    --speeds=-1,-0.5,0,0.5,1 --repeat 10 --log k6.txt
python -m modem.k6_harness listen --file k6.txt     # score a saved log again
```

**Which image is running.** Every image reports its build: the sotto commit it was built
from, then the UTC build time (`K6 BOOT build=72b6e3b-20261104T0555Z` at power-on, and
`build=` at the end of each `CFG` line). The commit also shows on the VOICE MSG screen's
fourth line, after `ok n bad n`. CI names the artifact `aura-pager-<commit>` and stores each
.bin's SHA-256 next to it. `listen` and `sweep` print the build when it changes, and every
`--log` line carries it. With `sweep --expect-build <commit>`, nothing is transmitted until the
K6 reports that build, and the sweep stops if the K6 reboots into another one. Images from
before this change report no build at all.

`listen` prints one line per packet:

```
#3    sync N  BAD  got 64  BE 111/512  s+x-  pkt  0  .....................................2534443446634624...  slip -1
```

- **The CRC result.**
- **`BE`:** the bit errors against the closest packet of the spec's test message.
- **The reading that fits:** `s` is the byte swap, `x` is the inversion.
- **The error map:** one character per byte, `.` when the byte is right, otherwise its number
  of wrong bits.
- **`slip`:** a slip of -1 means the K6 lost a bit at the point where the errors start, and +1
  means it gained one.

`sweep` keys the IC-7100 for each speed offset and level, and sends the test packets. Speed is
in percent, and baud and tones shift together, as a sound-card clock error would. It collects
the K6's reports and ends with a table of packets seen, CRC passes, mean bit errors and errors
per eighth of the packet. The K6's PHY, frames and baud are still set with its keys. `sweep`
refuses to transmit if the K6's last `CFG` line doesn't match (`--anyway` sends regardless). Press any setting key twice to make the K6
send a `CFG` line (every key sends one; **4** is the harmless one, it only toggles playback
pre-emphasis). From build 0a1fb98 on, the K6 also sends one every 10 s while idle (`why=tick`, not
within 2 s of a good packet). `--dry-run` listens without keying.

**64-byte receive, first on-air result (2026-11-04).** With FFSK from the IC-7100, the first
16 bytes of every 64-byte packet arrived with no bit errors at every speed (-1 to +1 %) and
level (0.2 to 0.7). After them, the FIFO only repeated an old word. Each PKT line's `ev` field
(`[S@0 F@27+4 ...]` in `listen`) shows when the sync, FIFO-almost-full and RX-done interrupts
came, and how many words each one read. Keys 7 and 8 try other length settings. All of
this is reported on the serial line, so watch it with `listen`.
If the playback's DMA doesn't move for 200 ms, the radio stops playback and goes back to
listening. It also writes `K6 PWM stalled ...` with the DMA and timer registers, so a PWM
path that doesn't run can't lock up the radio.
**First playback on the radio (2026-11-04).** After good 16-byte packets, the first
auto-playback hung the K6 before its playback loop, and only pulling the battery cleared it.
The image now prints `K6 STEP ...` lines through playback, so the serial report shows how far
it got. A Rust panic prints `K6 PANIC file:line message` and reboots the radio instead of hanging.
A CPU fault should print `K6 FAULT pc=... lr=...` and reboot. That only works if the
bootloader's HardFault handler forwards to ours, though: this MCU has no VTOR, and the
exception vectors belong to the bootloader. Either way, the last `K6 STEP` line says how far
playback got.

**Why 64-byte packets failed (2026-11-04, image 72b6e3b).** The `ev` timing showed the chip
receives the whole 64-byte packet: RX-done comes about 460-510 ms after the sync, as for 64
bytes. Its 0x1000 interrupt, which the firmware took as "FIFO almost full", comes about 14
bytes before the end of the packet as REG_5D sets it (F at about 50 bytes for 0x3F00, 18 for
0x1F00, 3 for a 16-byte packet), not when 4 words are waiting. By then the 8-word FIFO had
overflowed and kept only the first 16 bytes. Profile S worked because its whole packet fits in
the FIFO. The fix is to read the FIFO on a timer from the sync interrupt, one word per 13.3 ms
(16 bits at 1200 baud) with one word of lead, and read the rest at RX-done. That's now the
default (key 8 used to switch back to the interrupt reads; it now sets the re-arm delay). A
packet's `ev` ends in `T@ms+n` when the timer read its last word.

**Back-to-back packets (2026-11-04).** On 314a306 only 46 of VE9QRP's 64 packets were
caught, and each one that arrived had 0 bit errors. The losses are in when the firmware re-arms
the receiver after a packet read on the timer:

| Image | Re-arm | 16-packet test |
|---|---|---:|
| 314a306 | at the chip's own RX-done, or a 150 ms timeout that could run past the next sync | 12-13 |
| f413547 | at once, before the payload's end | 4-5 |
| 5d30ee8 | a fixed 60 ms after the payload's end | 4-6 |

A key 8 sweep on 5d30ee8 over VE9QRP (re-arm 0-120 ms after the end, 3 packets each) showed
two failure modes. Re-arming before the chip's own RX-done (0-30 ms) makes that RX-done come
late, 110-175 ms after the last word, and re-arming again on it lands on the next packet's
preamble and false-syncs (`gap` about 53, a FIFO interrupt every 13 ms). Re-arming 60 ms or
later leaves too little 0x55 for the chip's bit clock: the packet syncs on time but slips (about
240 bit errors, F early). 40 ms after the end, with the RX-done already in, was 3 of 3. A gap
test ruled out the preamble length: 53 or 100 ms of extra 0x55 caught no more, and 100 ms made
the chip false-sync, so the spec's 16-byte preamble stays.

So the firmware now re-arms once the chip's RX-done is in and at least 40 ms after the payload's
end, or 50 ms after it without the RX-done. A late RX-done after that is dropped without
re-arming, including one inside the next packet. That got 7-10 of 16. The rest synced 30-50 ms
late by the radio's clock (F about 300 ms after the sync instead of 340), so the timed reads
started late and slipped: the main loop checks the chip once a pass, between a 10 ms sleep and a
screen redraw every 50 ms. So after each packet the firmware now waits for the next sync in a
tight loop (every 0.5 ms, up to 250 ms after the re-arm), and the screen catches up after the
burst. On 6661d40 that got 16 of 16 and 14 of 16 on the test message and 63 of 64 on VE9QRP,
with 0 bit errors. But 6661d40 also froze while idle (keys and power switch dead until a battery
pull), most likely looping on false syncs in noise, so the loop now goes back to the main loop
after any packet that fails its CRC. The main loop also checks the chip every 0.5 ms through
its 10 ms pause, for the first packet of a burst. A `K6 PKT` line carries `raw=` only for a packet that failed its
CRC, to keep the line short between back-to-back packets (about 11 ms instead of 28), and the
re-arm delay is in bit times, so at 2400 baud it is half as many ms. Timed reads also follow the baud rate (a word
per 6.7 ms in D at 2400). Each `K6 PKT` line has `gap=` (ms from the last re-arm to this
packet; about 90 back to back, about 560 more per packet missed), `stray=` (when the last timed
packet's RX-done came, in ms after its last word; -1 for not before this packet) and `rearm=`
(the delay that preceded this packet). **Key 8** steps that delay (ms after the payload's end)
through 40, 50, 30, 60, 20, 0 and 80, then a sweep that moves it 10 ms per packet from 0 to 120
(`rearm=-1` in the CFG line). The PC can't set it: the cable's PC-to-radio wire is PA10, the
PTT input.

With REG_5D at 0x00FF (key 7 pressed twice), the chip instead streamed everything it
demodulated, preamble and sync word included, with a FIFO interrupt every 4 words and no sync
or RX-done interrupt. One 64-byte packet came through that way with 0 bit errors over 60 bytes.
That length setting is now a stream mode: the firmware reads 4 words per interrupt, finds the
sync word in software (byte aligned, up to 2 wrong bits, inverted too), and keeps the next 64
(or 16) bytes. The sync letter shows `W`, or `w` if the sync came inverted.

**Faster link (2026-11-04, after 6661d40/fb0a69e).** Three options (packet spec section 12);
only XL works on air so far, see below:

- **F at 2400 baud** (keys 1 and 5): FFSK 1200/2400 Hz, MSK at 2400 baud. It halves the air
  time if the chip's FFSK receiver follows the baud register. The calibration transmit now
  uses key 5's rate, so a PTT press with F/2400 set, recorded on the IC-7100, shows what the
  chip really sends.
- **Profile XL** (key 2): 128-byte packets with 20 frames, 120 of 128 bytes speech (L: 48 of
  64). Timed reads and the re-arm rule work for it as for L.
- **Stream mode for whole messages** (key 7 pressed twice, or three times for REG_5D 0xFFE0):
  the chip is armed once, and the firmware searches every bit for the sync word, takes the
  payload and searches again, so the sender (`modem.k6 --stream`) puts only a sync word between
  packets: no per-packet preamble and no re-arm. While a signal is on (a run of preamble, a
  sync or a good packet in the last 1.5 packets) poll stays in a 0.5 ms loop, since the
  screen redraw would let the FIFO overflow at 2400 baud; in noise it returns after one look.
  An RX-done or 300 ms without a FIFO interrupt while a signal is on re-arms the chip and
  counts in `restarts=` (screen `RS`); idle, it re-arms after 1 s without one. The old
  per-packet stream reading (byte-aligned search, re-arm after each packet) is gone.

On air (e2411fe, F/L/1200): stream mode caught 0 of 16 back-to-back packets and 1 of 16 with
preambles, after 26 re-arms. Each re-arm in stream mode now prints `K6 SRST why= ms= bytes= full=
syncirq= mind= pre= collect= head= n=`: D = the chip's RX-done, S = a stall with a signal on, I
= a stall while idle; `mind` is the fewest wrong bits against the sync word seen in any bit
position since the arm, `pre` how many bit positions looked like preamble, `head` the first 8
bytes after the arm. The same image showed packet mode losing the packet after a failed one
(11 of 16): the burst loop left on the first failure; it now leaves after two in a row.

F/2400: the K6 transmits it right (the PC decoded its CAL burst, 1200/2400 Hz tones, 280 ms per
packet, preamble and sync bit-identical to ours) but never synced on the PC's F/2400 in receive
(f190a46, b0ff399, b2ecda4): not at levels 0.1-0.7, with two tilts, with a 1 s lead-in and
128-byte preambles, with REG_58 0x73E9, 0x7329, 0x73E3, 0x7FE9, 0x70E9 or 0x63E9, nor with the
AF receive filters off (REG_2B +0x0100/+0x0300/+0x0700). D/2400 neither. Parked.

**What works: XL at F/1200 (b0ff399, b2ecda4).** The 8-packet XL test message 8/8, 0 bit
errors, once the softmodem gives every packet after the first 24 bytes of preamble (16 before
the first: a longer run of 0x55 before the first sync made the chip false-lock; 16 after a
128-byte packet left the re-arm in the next preamble when the chip's late RX-done came, about
144 ms after the timed end). A 12 s clip (female_libri198): 580/600 frames in 33.4 s, twice
with seq 0 the lost packet; with `modem.k6`'s new default of sending the first packet again at
the end, **600/600 in 34.5 s** (L: 45.0 s). Packet mode F/1200 L: 16/16 twice once the
softmodem added 50 ms of preamble before the second packet (seq 1 had always been lost: the
K6 re-armed late after the first, which its main loop notices during a screen redraw).

For a 12 s message: F 1200 L 45 s; **F 1200 XL 34.5 s (tested)**; F 1200 XL stream 28 s and
F 2400 L 23 s (not working yet). Profile S (16 bytes) is removed from the firmware and the C
library; the PC softmodem still makes it.

**Playback hang found (2026-11-04, image 480d312): the stack ran out.** When playback started,
the stack pointer was only 236 bytes above the end of static RAM. Aura had used 8.3 KB of the
8.5 KB stack (its `App` and the main loop live on the stack), and the decoder needs about
3.5 KB more. Frame 0 overran into static RAM and frame 1 never finished. The fix moves the
message store into the RAM below UserVectors (`RAM0` in memory.x), which Aura leaves unused.
The first try used all 4 KB from 0x2000_0000 and hung at boot, at the Aura main screen, when
the store was cleared. The bottom of that RAM evidently holds the live interrupt vector table.
A second try from 0x2000_0100 hung the same way, so that RAM is in use and the store is back
in Aura's RAM. At power-on the image prints `K6 VEC` with the first 20 words at 0x2000_0000.
`build.sh` now lists the largest stack frames (`frames-*.txt`, from `tools/k6_frames.py`), to
find the room in Aura's own RAM. `build.sh` reports RAM0's use, and warns when under 12 KB is left for the stack.

**Timed reads on air (image 480d312).** The first full-length packets passed: 3 of 10 had
64 bytes, 0 bit errors and a good CRC. Each time, the chip's own RX-done for the packet came
after the timer had read it, and the firmware took it as a new packet, which put the next
one out of step. The firmware now waits for that RX-done after a timed packet and drops it.
It also ignores an RX-done that comes without a sync.

**Second playback test (2026-11-04, image 72b6e3b).** 34 of 40 profile-S packets arrived, all
with clean CRCs, and the radio stopped inside the decoder: after `first frame decoded`, before
the four-frame line. The PC decodes the same 34 packets in 4 ms. The next image prints, for
each of the first 6 frames, a `K6 STEP frame n pos=` line before it and `done ... us= stack_free=`
after it, plus the stack pointer, the stack floor and the least free stack at playback start.
Key 0 adds a SysTick watchdog around each decode. It relies on the bootloader forwarding
SysTick through UserVectors[0], as it forwards USART1 through [1], which isn't confirmed yet
(the stock firmware uses that slot). So it is off by default. When it's on, a frame that
takes over a second prints the interrupted pc and lr and reboots. A stop with no `K6 HANG`
line points to a fault, because a HardFault outranks SysTick.
Key 9 turns auto-play off, so receive tests can run without it.
