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
   with the speaker muted, on the frequency stock Aura last saved. The pager-only image writes
   nothing to flash, not even at power-off.
2. **Set the frequency** with **#**, then six digits in kHz, the same way as Aura's VFO entry.
   For example, `# 4 3 9 5 0 0` gives 439.500 MHz, and `# 1 4 6 5 2` then MENU gives 146.520 MHz
   (MENU fills the rest with zeros). Below 100 MHz, start with a 0. EXIT deletes a digit, or
   cancels when nothing has been typed. The frequency is kept in RAM only, so at power-on the
   radio is back on the frequency Aura saved. To change that default, set it in stock Aura. The
   bandwidth (wide or narrow) and offset are Aura's saved settings, too. FM wide is the usual
   setting.
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
| 1 | PHY: D (direct 2-FSK, the default) or F (FFSK 1200/1800 Hz) |
| 2 | frames: L (64-byte packets, the default) or S (16-byte fallback) |
| 3 | the RF chip's RX invert bit (REG_59<10>), by hand only |
| 4 | pre-emphasis on/off (undoes the PWM filter's -10 dB at 3 kHz) |
| 6 | raw view: the last packet's first 16 bytes in hex, its sync and length, and its bit errors against the test message (below) |
| 5 | 1200 / 2400 baud (profile D) |
| 7 | bring-up: how the packet length goes into REG_5D (bytes - 1, words - 1, or the longest), serial report only |
| 8 | bring-up: keep reading after an early RX-done until the whole packet came, serial report only |
| 9 | auto-play on/off (on by default; MENU still plays) |
| * | clear the message |
| # | type a frequency (kHz, six digits; MENU sets it early, EXIT deletes) |
| PTT | calibration transmit: test packet 0, twice in D and then twice in F, on the VFO's TX frequency and power |

The screen:

```
VOICE MSG  439.50000     frequency
D L 1200 i-x-s+ e+ +0    PHY, frames, baud, chip invert, soft invert, byte swap, emphasis, dB
ID 42  640/640  80/80    message id, frames received/stored, packets received/total
SYNC P CRC ok A7 TO 0    last sync polarity, CRC, magic byte (A7 = right), FIFO timeouts
DEC 11.8/17.2ms 2930     after a playback: decode ms per 20 ms frame, mean/worst; free RAM bytes
MENU play  PTT test
```

A message holds up to 640 frames (12.8 s). `STORE_BYTES` in `src/app/pager.rs` sets that, at 6.2
bytes per frame. Raise it if the free-RAM figure leaves room.

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
     deviation, or press 1 for profile F. Lower-case `p`/`n` mean the sync interrupt didn't fire
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
warns if the K6's last `CFG` line doesn't match. Press any setting key twice to make the K6
send a `CFG` line. `--dry-run` listens without keying.

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
it got. A CPU fault prints `K6 FAULT pc=... lr=...` and reboots the radio instead of hanging.
Key 9 turns auto-play off, so receive tests can run without it.
