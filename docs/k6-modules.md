# K6 RAM modules: native code over the air

The K6 pager firmware can take a small native program over the air, check that you signed it,
and run it once from RAM. Nothing is written to flash. The first module, `c/k6/mod/hello.c`,
prints where it is running and times a small loop, all on the serial report. The design memo
(prior art, memory map, later stages) is the Claude Doc "Modular K6 firmware with loadable
modules".

## What happens

1. The PC builds the module with clang for the Cortex-M0 using `-fropi`, so its code and
   constants are reached PC-relative and it runs at any address. `modem/k6mod.py` links it at
   two addresses and refuses it unless the bytes come out identical.
2. It adds a 16-byte header and an 8-byte SipHash-2-4 tag under your 16-byte key, cuts the
   image into packets with magic `0xAB`, and makes transmit audio like any other packet.
3. The K6 (`c/k6/k6_mod.c`) puts the chunks straight into the firmware's shared RAM block
   (below), in any order, repeats ignored. Once all are in, it checks the header, the tag and
   the serial, then calls the entry function with a table of services. The message store is
   set up again afterwards, so any message held before the module is gone.

**How much room.** Images built from 2026-11-08 on keep the message store (4 KB), the PWM audio
ring and the voice decoder's working memory side by side in one block (`hal::arena` in the Aura
patch), because a module never runs while a message plays. A module gets the whole block: in
every image that reads its codebooks from SPI flash (`aura-pager-only`, `-lr`, `-vlr`, `-vlrh`)
it is 9,760 bytes, so the header, code and tag can be up to 9,736 bytes, and whatever the code
leaves is the module's scratch. The serial line `K6 ARENA bytes=9760 ... dec=... need=...` at
power-on shows the layout. The EXPLORE image and the lean images (codebook install, codebooks in
MCU flash) keep the store plus the ring: 5,376 bytes. Before
2026-11-08 every image gave modules the 4 KB store only.

The hello module is about 360 bytes of code (366 with clang 18 here, 358 in CI), 4 XL packets,
3.1 s on air at F/1800 per copy; `modem.k6 mod` sends every chunk twice by default.

**On air, 2026-11-05** (image f6b0d2a keyed with `keyimage`, F/1800 XL, level 0.3): the first
send lost chunk 0 (bad CRC, the burst's first packet) and chunk 1; `--only 0,1 --copies 2`
filled them in, and the K6 reported `run ... at 0x20001bfc`, `say hello world from RAM at
0x20001bfd` (the Thumb bit is set), `sum of squares 0x9f31fc58 in 0x000e ticks`, `ret=107
(0x6b) in 11.8 ms`. A sotto 1200 message afterwards played with a matching `pcm=` checksum.

## Packet and image format

De-whitened (PN9, as voice packets), an L or XL module packet is

| Bytes | Field |
| --- | --- |
| 0 | `0xAB`, or `0xAE` for the same chunk with a reply wanted (upload ARQ, below) |
| 1 | transfer id |
| 2 | chunk number, from 0 |
| 3 | chunk count (1-255) |
| 4 .. len-3 | data: 58 bytes in L, 122 in XL |
| len-2, len-1 | CRC-16/CCITT-FALSE of bytes 0 .. len-3, big-endian |

The chunks' data, in order, is the image (the last chunk padded with zeros):

| Offset | Field |
| --- | --- |
| 0 | `K` `6` `M` and ABI major `1` |
| 4 | code length, u16 little-endian |
| 6 | entry offset in the code, u16, even |
| 8 | serial, u32: must be newer than the last module this boot ran |
| 12 | API level the module needs (1 to 7; a K6 refuses a newer one), flags (0), telemetry parts (0-12, level 4 images; see below), 1 reserved byte |
| 16 | code: Thumb, position-independent, no writable statics |
| 16 + code length | SipHash-2-4 tag of bytes 0 .. 15 + code length, u64 little-endian |

The entry is `int k6_main(const k6_api_t *api)`. API level 1 (`c/k6/k6_mod.h`) has `say`, a
serial line `K6 MOD say <text>` of up to 60 characters, and `ticks`, the 100 us uptime
counter (16 bits). The table only grows: a later level appends services and keeps these.

API level 2 (images from 2026-11-06 on) adds the receiver:

| Call | What it does |
| --- | --- |
| `fd_read(reg)` | an FD6818 register |
| `fd_write(reg, v)` | writes one, 0; or -1 for the registers it refuses: 0x30 (RX/TX state), 0x33 (GPIO), 0x36 (PA), 0x37 (power). In images from 2026-11-06 06:30 UTC on, a 0x33 write that changes only bits 8 and 0 (GP0, the line to MCU PB1) is allowed, and the firmware puts 0x33 back after the module |
| `delay_us(us)` | busy wait, any length |
| `tune(hz, wide)` | receive on `hz`: band switch, PLL, wide or narrow filter, RX on |
| `rx_hz()` | the frequency the pager listens on |
| `ms()` | ms since the module started, 32 bits |
| `key_down()` | 1 while any key is held |
| `scratch(&bytes)` | RAM free while the module runs: the rest of the shared block past the code and tag (up to the telemetry text, if the module keeps one), word aligned, its size in `bytes`. About 9.7 KB less the code in the SPI images; 4 KB less the code before 2026-11-08 |

Level 2 is receive only: there is no way to key the transmitter. After the module returns the
firmware puts the radio back on the pager's frequency and sets the FSK receiver up again,
whatever the module changed. `KM_RSSI_DBM(fd_read(0x67))` is the RSSI in dBm. A module that
uses level 2 says so with `#define K6_MODULE_LEVEL 2` before including `k6_mod.h`;
`modem.k6 mod` reads it and puts it in the header (`--api-level` for a `.bin`).

## API level 4: upload ARQ, telemetry, calibration

Images from 2026-11-06 evening on (`KM_LEVEL` 4) add three calls and two over-air services. Level
3 (`tx_on`, `tx_pa`, `tx_off`: docs/k6-ft8-wspr.md, PR #71) is unchanged.

| Call | What it does |
| --- | --- |
| `key_poll()` | 1 while any key is held, from one GPIO read with all rows driven (a few us; `key_down` scans six rows, about 60 us). The lean images fall back to `key_down` |
| `cal_read(buf, n)` | the first `n` (up to 64) bytes of the calibration sector, SPI flash 0x10F000; n, or -1 (lean images) |
| `cal_write(buf, n)` | erases that sector and writes `n` bytes; once per run and never while keyed; n, or -1 |

**Upload ARQ.** A module sent with magic `0xAE` (`modem.k6 mod --arq`) asks for a reply. When
the transfer stops (2 s after the last packet heard, and the channel below -90 dBm), the K6
keys up on 440.850 MHz only, at low power, and sends a 64-byte report twice in the pager's PHY
and baud (F/1800 by default), then an MCW ID (AG7EW at 25 wpm, on the tone oscillator). It
sends at most 16 reports a transfer and waits up to 30 s for a busy channel, then gives up.
The serial log shows `K6 ARQ tx ...` and `K6 ARQ sent ...` around each.

| Report byte | Field (de-whitened, CRC-16 of bytes 0-61 at 62-63 as module packets) |
| --- | --- |
| 0-4 | `0xAF`, kind (1 status, 2 text), seq, part (0 for the status), parts (1 + text parts) |
| status 5-13 | transfer id, chunks held, chunk count, last `km_packet` result, state (0 partway, 1 ran, 2 refused), serial (once checked) |
| status 14-45 | while partway: a bit per chunk still missing, chunk 0 in bit 0 of byte 14 |
| status 46-61 | call sign (6 bytes), the module's return value (i32), its run time in ms (u32), free stack (u16) |
| text 5-61 | 57 characters of the module's `say` lines, newline-separated |

A partway report lists the missing chunks, so the sender resends just those with the same
serial and transfer id. A repeat of a chunk after the run asks for the report again.
`tools/k6_upload.py` does the whole loop on hamlet: it plays the upload on the IC-7100,
records the IC-7100's receive audio with `parec`, decodes the reports (`modem.k6 reports
REC.wav` does the same for any recording), resends what's missing, and stops on "ran" or
"refused".

**Telemetry.** `--telemetry N` (with `--arq`) puts N in header byte 14: while the module runs,
the K6 keeps its `say` lines (up to N x 57 bytes, at most 12 parts) in the top 696 bytes of the
shared block (the idle PWM audio ring before 2026-11-08), which the module's scratch then stops
short of, and sends them after the status report. No serial cable is needed, so the speaker stays on.

**Calibration.** `c/k6/mod/ifcal.c` (level 4) measures where a bare carrier on 440.850 lands on
the GP0 IF with the AFC off and writes a 32-byte record (`c/k6/mod/k6_cal.h`: magic `K6CL`, the
offset in 0.01 Hz from the nominal 16927.083 Hz, the same in ppb, the frequency, the block count,
spread, RSSI, and a check word). An IF-tap module can read it with `k6cal_get()` and scale it to its
own frequency with `k6cal_off_chz()`. The FT8 front end does (`gp0rx.h`: ft8wf, ft8wf_inv,
ft8rx, which print `cal=` in 0.01 Hz on their start line, 0 for none), so FT8 goes at its true
audio frequency instead of an `--f0` aimed by hand. iflive finds the offset itself with its
sweep and AFC; the softmodem thread will seed it from the record later. A module that reads the
record still declares the level it needs otherwise: `k6cal_get()` checks `api->level` first,
so on an older image it just mixes at the nominal IF. Where it lives: 0x10F000 is a 4 KB sector
of its own, after the v3.7 codebook (0x100000-0x107820) and sotto 600 / 400's (0x108000-0x10D320),
outside the repeater table (0x0F0000-0x0FFFFF) and above everything Aura uses (to ~0xED200).
`ifcal` writes it only if it is blank or already holds a `K6CL` record (else -11, nothing
measured or written). Run it once per radio: within 90 s of `ifcal start`, key
the IC-7100 on 440.850 in FM with no modulation for about 8 s. It refuses (-9) a carrier that
wanders more than 30 Hz or fewer than 16 good blocks, and the record stays as it was. Before
deciding, it lists every block's IF (`ifcal b0 16763 +1 -2 ...`: the first in Hz, then each
block's step from the one before), so a refusal shows whether the carrier drifted, jumped or
was noisy.

The lean images (`aura-pager-cbi`, `-only-table`, `-lr-table`, `-vlri`) have no room for any
of this: they take level 4 modules but send no reports, keep no telemetry and refuse
`cal_write`.

## API level 6: the module's screen

Added 2026-11-07 for the WSPR modules (Jeff: "Design a module with nice UI"), after level 5's
flash calls (`docs/k6-explore-app.md`). Until then a module could only talk over the serial
report; the screen was Aura's. The firmware keeps the state and owns the font and the display;
a module sends only text and pixel rows (`c/k6/k6_mod.h`):

- `lcd_text(line, s)`: six lines of 21 characters in FONT_6X10 at baselines y = 9, 19 .. 59
  (the explorer screen's layout), line 0 drawn as an inverted title bar;
- `lcd_bar(line, num, den)`: a progress bar after that line's text;
- `lcd_wf(ring, rows, head)`: up to 30 rows of 128 pixels (16 bytes each, MSB leftmost) from
  y = 21 down, in place of lines 2-4, `ring[head]` on top. The rows stay in the module's
  memory, so the firmware needs no buffer for them;
- `lcd_flush()`: draws the whole frame and sends it to the display (about 20-40 ms; 0, or -1
  with no display: the lean images leave the screen out, 1 KB they don't have, so a module
  there keeps to its serial lines).

The firmware's part is in the Aura patch: `ModScreen` and the four calls in `app/pager.rs`,
`draw_mod_screen` in `ui/pager.rs`, and a hook `main.rs` registers once with the display
(`ui::hook_mod_screen`), since modules run inside the main loop's calls and never while it
draws. When the module returns, the pager's own screen comes back. About 190 B of RAM.

**One build for old and new images.** A module reads `api->level` at run time and calls the
screen only when it is 6 or more; its `K6_MODULE_LEVEL` stays what its other calls need. So
`wsprtx` (level 3) and `wspriq` (level 2) run on today's images without a screen and draw one
on a level 6 image. The emulators (`tests/test_k6_i2s.py` `run(lcd=...)`, `tests/test_k6_ft8.py`
`emulate(lcd=...)`) record each flushed frame, and fail a screen call below level 6.

## API level 7: the green RX LED

Added 2026-11-08 (Jeff: "indicate modem receive activity by blinking the green LED in the front
of the radio by the volume knob"). The LED is MCU pin PA3 (Aura's `G_LED`, push-pull, active
high; stock Aura lights it while the squelch is open, which main skips in pager mode, so in
VOICE MSG the pin is free). One call, `led(on)`: 1 lit, 0 dark. The core puts it out when the
module returns.

The pager itself blinks it once a packet while its modem receives (no module needed):

- **Chip FSK packets** (PHY F and D: voice messages, control packets, module chunks): lit at a
  packet's sync (or its first FIFO-full), dark 120 ms later. A burst at PHY F (a packet every
  391 ms) blinks about 2.5 times a second. A false sync on noise blinks it once.
- **The coded 4FSK session** (PHY G, `c/k6/ifpager.c`): lit at each sync word, dark 144 symbols (120 ms)
  later, through the same `api->led` (the session gets the module table); a packet every
  0.92 s at rate 1/2. The check runs with the session's key poll (every 256 counts, about
  3 ms), away from the receiver's bursts, and keeps no state (it sets the LED from the last sync
  word each time: the session's stack is at its test limit). `test_session_blinks_the_led_once_a_packet` checks
  the pattern on the emulated M0 and that the session keeps its CPU margin.

A module reads `api->level` and calls `led` only on 7 or more, as with the screen; the
emulator (`tests/k6_live_emu.py`, `api_level=7`) records each call as `(ms, on)`.

The lean images (`aura-pager-cbi` and the table images) keep the LED dark and their `led` is a
no-op: cbi had 96 B of flash to spare and the LED took 184 (CI, 07bb380). Gated off it costs
28 B there. The other images pay about 300 B.

## Security

- **Signed.** A module runs only if its SipHash tag checks under the key the image was built
  with. SipHash-2-4 is a keyed MAC; forging a tag means guessing 64 bits, one transmission
  per guess.
- **No key, no modules.** An image built without `K6_MOD_KEY_FILE` (CI's) holds a placeholder
  and refuses every module (`r=-5`) until `modem.k6 keyimage` writes your key over it. A keyed
  image holds the key: don't publish it, or the key file.
- **Replays.** The K6 refuses a serial that isn't newer than the last module it ran since
  power-on (`r=-6`). After a reboot a recorded transmission of an old module of yours would
  run again; that's the gap a boot nonce would close later.
- **The module is trusted code once it runs.** The M0 has no MPU and Aura has no watchdog
  yet, so a bad module can hang the radio until a power cycle. Recovery is the usual one:
  power off and on, or reflash.
- **Part 97.** The code goes out in the clear and this format is published; the tag only
  proves who sent it.

## Running it

On the machine that builds the K6 image and drives the IC-7100:

```sh
python -m modem.k6 modkey ~/.config/sotto/k6-mod.key      # once
K6_MOD_KEY_FILE=~/.config/sotto/k6-mod.key sh firmware/k6-aura/build.sh
# flash out/aura-pager-lr.bin (sotto 1200) or out/aura-pager-only.bin, e.g. tools/k6_update.py
# (out/aura-pager-cbi.bin once first, on a radio without the codebook in SPI flash)
python -m modem.k6 mod hello.wav c/k6/mod/hello.c --key-file ~/.config/sotto/k6-mod.key \
    --phy F --payload XL
# key the IC-7100 and play hello.wav as for any other packet WAV
```

Without a toolchain, use the CI artifact of the *K6 pager firmware* job instead. Its images
carry a 16-byte placeholder where the key goes, and `mod-hello.bin` is the module's built code:

```sh
python -m modem.k6 keyimage aura-pager-lr.bin keyed-lr.bin --key-file ~/.config/sotto/k6-mod.key
# flash keyed-lr.bin
python -m modem.k6 mod hello.wav mod-hello.bin --key-file ~/.config/sotto/k6-mod.key --phy F --payload XL
```

The K6's serial report then shows, for a good run:

```
K6 MOD r=1 xfer=.. chunk=0 have=1/4
K6 MOD r=1 xfer=.. chunk=1 have=2/4
K6 MOD r=1 xfer=.. chunk=2 have=3/4
K6 MOD r=3 xfer=.. chunk=3 have=4/4
K6 MOD run serial=... code=366 B at 0x2000.... stack=... B scratch=... B
K6 MOD say hello world from RAM at 0x2000....
K6 MOD say sum of squares 0x9f31fc58 in 0x.... ticks of 100 us
K6 MOD ret=107 (0x6b) in .. ms stack_free=... B
```

`r` is `km_packet`'s result: 1 a new chunk, 2 a repeat, 3 the module checks and runs, -1 bad
CRC, -2 a chunk out of range for the store, -3 bad header, -4 wrong key, -5 no key in this
image, -6 a replay. If chunks are missing, send just those again with the same serial and
transfer id: `--only 2,3 --serial N --xfer X` (the first send prints both). A good voice packet
in the middle of a module transfer drops the transfer (`K6 MOD dropped`).

## Writing a module

Copy `c/k6/mod/hello.c`. The rules, which the build checks where it can:

- No libraries: no `memcpy`, no `printf`. The M0 has no divide instruction, so no `/` or `%`
  on variables either (clang would call a run-time helper the module doesn't have).
- No writable statics (`.data`, `.bss`): the linker script refuses them. Use the stack, and
  keep it small: the module runs on the pager's stack, with about 700 B to spare. Level 2's `scratch()` gives the rest of the shared block
  for tables and buffers (about 9.7 KB less the code). `K6 MOD run` reports the stack room at the call (`stack=`) and the scratch size,
  and `K6 MOD ret` the stack never touched since VOICE MSG started (`stack_free=`).
- No tables of pointers: they need relocation, and the two-address check refuses them.
- Return quickly, or give a way out: the pager doesn't receive while the module runs. A
  long level 2 module polls `key_down()` and stops on a key.

Tests: `tests/test_k6_mod.py` (the loader on this machine with a test key, the thumbv6m
loader running the hello module in an emulated Cortex-M0, and the settle module against a
modelled receiver).

## The settle-time module (explorer phase 0)

`c/k6/mod/settle.c` measures how long after a retune the FD6818's RSSI reads true, the number
every sweep speed in the explorer design rests on. With a steady carrier on the pager's
frequency F, for each way of arriving at F (from F - 10 kHz, from F - 1 MHz, from the other
band; and leaving, F to F + 1 MHz), at wide and at narrow, it parks 5 ms on the start
frequency, tunes, waits t and reads RSSI, for t from 0 to 20 ms, four times each. The settle
time is the first t from which every reading is within 2 dB of the reading after 30 ms.

```sh
python -m modem.k6 mod settle.wav c/k6/mod/settle.c --key-file ~/.config/sotto/k6-mod.key \
    --phy F --payload XL
# play settle.wav to the K6 as usual; when the report says "waiting for a carrier",
# key the IC-7100 on the same frequency (FM, no modulation) for about 10 s
```

It waits up to 60 s for the carrier (RSSI 15 dB over where it started), and any key stops
it. The report, for each case:

```
K6 MOD say settle 10k W ref -61 dBm 700 us
K6 MOD say curve 10k W 0:-118 100:-118 200:-118 400:-118 700:-61
K6 MOD say    1000:-61 1500:-61 ...
...
K6 MOD say settle: done in 6328 ms, worst on-case 1500 us
```

`curve` is the mean RSSI (dBm) at each t (us). `probe on|off` lines dump the noise, glitch
and AF registers (0x63 0x64 0x65 0x6f 0x4f) and the status (0x0c) with the carrier on and off,
for the next step, the noise-based quick check. The return value is the worst settle time
in us of the cases that arrive on the carrier, -1 if no carrier came, -2 if a key stopped it.

## The receive coverage module (rxmap)

`c/k6/mod/rxmap.c` sweeps RSSI across a segment (the K6 receives only) or measures the IC-7100's
carriers on 2 m / 70 cm points. `tools/k6_rxmap.py` patches the segment into CI's
`mod-rxmap.bin`, uploads it and decodes the over-air summary. See [k6-rxmap.md](k6-rxmap.md).

## The RAM probe (ramprobe)

`c/k6/mod/ramprobe.c` (2026-11-07, the hiss band thread): Aura's `memory.x` gives the application
0x20001028 + 17720 bytes, ending at 0x20005560, but the KD32F328 is sold with 24 KB of SRAM, which
would end at 0x20006000. Nobody has claimed those 2720 bytes. The module writes two patterns to
every word of that range, holds a third for 50 ms, restores what it found, and says
`ram probe good=2a8 bad=none held=2a8 alias=none` (0x2a8 = 680 words = all of it; alias = a lower address that moved with the probe, a mirror of a smaller SRAM; on 2026-11-07 the K6 said good=2a8 bad=none and ran on, so the RAM is there, all of
it). If the SRAM ends lower the first access past the end faults and the radio resets, so run it
when a reset is acceptable. If it holds, `memory.x` can grow by 2720 bytes: the hiss band image
(docs/k6-hiss-port.md) would get its fourth PWM segment and its 4 KB message store back.

## Band surfing (surf)

`c/k6/mod/surf.c` (2026-11-08, level 6): a bargraph of signal strength across 64 bins, the
listening frequency a blinking bar with a caret, the side keys jumping from peak to peak, UP/DOWN
stepping, BAND/zoom/monitor keys. It listens first (sweeps only while the bar's channel is
quiet) and scans the keypad matrix itself, since the API has no key identity. `surfauto.c` is
the unattended bench build. See [k6-surf.md](k6-surf.md).

## Insights (a living list)

Ideas from the K6 work that might change the plan. None has been tested unless it says so.
Later threads add to this list: each entry says where it came from, what it would buy, and
what would settle it.

1. **Receive audio through the FD6818's I2S port.**
   - From: the CW ID thread, 2026-11-06 ([k6-fd6818-audio.md](k6-fd6818-audio.md)).
   - The guide's MCBSP/I2S port can output FM-demodulated audio on a GPIO, and the K6 wires
     FD6818 GP0 to MCU PB1 (the CTCSS net; pin 19, not PB0). Only one wire, so the MCU would
     have to recover the clock itself. The BK4819 map may not have the block.
   - Buys: received audio samples with no hardware change, so a software modem past the chip
     FSK's 2400 baud slips, maybe live sotto 2400. It gives no mic samples.
   - Settled by: the I2S probe thread (`i2sprobe`, a module that puts the frame clock on GP0
     and counts PB1 edges). Result (2026-11-06, PR #68): no I2S port, but GP0 output code 13
     is a 1-bit sigma-delta stream of the receive low IF (about 17 kHz on a carrier, moving
     with the deviation), readable on PB1. Run 5 (`gp0fm`) settled the next question: 28 ms of
     IF decimated on the MCU demodulated on the host to a clean 1000.9 Hz tone at +-4.3 kHz
     deviation, about 25 dB SINAD. PR #69 then did it on the MCU: DMA channel 1 reads GPIOB IDR
     flat out (about 5.5 MS/s; TIM17 can't pace it), and `gp0dma` demodulated a 1 kHz tone on
     the K6 for 3 s continuously (996-1006 Hz, ±4.1 kHz, nothing lost, 27% of the CPU). Open:
     a software modem on that path, and whether the always-on DMA hurts the pager's own work.
2. **The tone detector as a slow MFSK receiver.**
   - From: the CW ID thread (on air it follows a keyed tone to about 10 ms and says which of
     16 tones it is).
   - 16 tones with 30-40 ms symbols is roughly 100-200 bit/s. Plain audio tones should pass
     any FM repeater and hold on to weaker signals than FFSK.
   - Buys: a fallback when packets don't get through: control and wake-up packets, short
     pages, beacons.
   - Settled by: sweeping signal level and comparing where `cwlisten` stops decoding with
     where FFSK packets start failing.
3. **A repeater fingerprint from one kerchunk.**
   - From: the CW ID thread.
   - The CW ID gives the call, tone and speed; the chip's CTCSS/DCS scan (REG_68/69 in UV-K5
     firmware) gives the PL tone.
   - Buys: one listener module that fills a table entry's call, PL and CW tone/speed. The
     tone and speed pair identifies a repeater even when the call won't decode (table byte 51
     has room).
4. **Repeater tail timing.**
   - From: the CW ID thread.
   - Time the courtesy beep and hang time with the tone detector and squelch.
   - Buys: the pager waits for the tail to drop before sending, instead of doubling with the
     repeater.
5. **A per-packet link log.**
   - From: the CW ID thread (REG_67 RSSI and REG_65 noise read cleanly every 2 ms; on air
     -91 to -5 dBm and noise 7-80).
   - Buys: telling fades, false syncs and clock slips apart, which is the data the 2400 baud
     work lacked. Later it could set the interleave depth automatically.
6. **A faster module workflow.**
   - From: the CW ID thread (every module cost a 43 s upload, and modules started while the
     upload's repeat copies were still on the air, which caused false decodes).
   - Keep modules in SPI flash and start one with a 2 s "run module N" packet.
   - Make the loader wait for the carrier to drop before km_run.
   - Optionally have the K6 acknowledge chunks. That needs the K6 to transmit, which is
     Jeff's call.
7. **If a hardware mod is ever acceptable.**
   - From: the CW ID thread.
   - An RC tap from EARO (or the speaker node) into a free ADC pin gives 8 kHz receive audio.
     It would also let the K6 measure its own audio passband, to put the FFSK tones on the
     flattest part. Jeff has ruled out hardware mods so far.
8. **A softmodem on the GP0 IF instead of the chip's FFSK.**
   - From: the softmodem thread, 2026-11-06 ([k6-if-modem.md](k6-if-modem.md), simulation only).
   - A matched filter on the complex IF beats the best FM discriminator by 5-6 dB, because it
     has no FM threshold. 4FSK at 1200 baud with soft rate-1/2 coding carries sotto 1200 about
     7.7 dB better than FFSK through a discriminator, and 2400 baud MSK is a dead end on the
     IC-7100.
   - Buys: about 2.4x the range for live voice, and no chip bit-clock slips.
   - Settled by: `gp0cap` with `tools/k6_ifcap.py`, which runs both detectors on the same
     captures at stepped front-end gain.
9. **More IF gain against the 1-bit floor.**
   - From: the same study. GP0's sigma-delta adds its own noise. If the receiver's noise sits
     only "a few %" above it on GP0, 1.5-4 dB is lost.
   - Buys: those dB back, by running REG_13 above the AGC table's top entry (for example
     0x03FF against 0x03BE) while the signal is weak.
   - Settled by: `gp0cap` pass 0 (the floor at three gains) and steps 1 against 2.
10. **A weak-signal mode: constant-envelope MFSK on SSB.**
    - From: Jeff's PSK idea, simulated at equal peak power (`sim/k6if/psk.py`). Differential
      PSK only ties FM-FSK with the IF detector, because RRC PSK loses 3.4 dB to its peak.
      16 tones at 100 baud from the IC-7100 in USB gain about 3.8 dB per bit and need no
      carrier sync. Coherent BPSK with a perfect carrier loop is the stretch goal (+3.7 dB
      coded).
    - Buys: beacons, text and control at 200-400 bit/s, several dB below what FFSK can reach.
    - Settled by: a third `gp0cap` pass with SSB 16FSK, decoded on the host.
11. **True SSB and a noise reference from the mirror band.**
    - From: Jeff found the chip's "USB" is DSB. Its USB mode is zero IF, so the sidebands fold.
      On GP0's 16.9 kHz IF they don't, so the K6 can tell the sidebands apart in software. The
      empty side also gives a live noise level for soft decisions and squelch.
    - Settled by: a capture of an SSB voice signal through `gp0cap`, demodulated on the host.
12. **A faster count loop.**
    - From: the study's CPU budget. `gp0dma`'s count loop takes 27%, the IF detector about 10-20%,
      and the sotto decoder 55-63%. A word-at-a-time masked-add loop (about 1 cycle a sample)
      would bring the counting to about 6%.
    - Settled by: timing such a loop in a module (TIM17 counts cycles), as `gp0dma` already does.
13. **Never move a symbol clock backwards in a streaming DMA receiver.**
    - From: `iflive` in the emulator (2026-11-06). The acquisition sweep wrapped its clock back
      11 steps (0.92 symbol), and the receiver then had about 300 us of samples to work
      through at once. The DMA ring lasts 186 us, so 15 counts were lost at each wrap. Moving
      forward to the same offset a symbol later costs nothing.
    - Buys: no lost counts. The same applies to any module that re-times a stream (FT8, CW).
    - Settled: yes, on the emulator (`tests/k6_live_emu.py` reports lost counts).
14. **Re-acquire per step instead of timing pauses.**
    - From: `iflive`. It starts the receiver over on a fresh lead every 6 s step, so it can
      print and change gain between steps without the tick + TIM17 lap reckoning `gp0cap`-style
      modules need. It saved about 200 B of code, and each run checks acquisition 10 times.
    - Buys: simpler bench modules wherever the sender can repeat a preamble.
    - Settled by: the first `iflive` bench run (every step should lock, `j=0`, with `b` near the step's full length).
15. **The IF receiver's CPU.**
    - From: `iflive` on the emulator at 1.5 cycles an instruction: 4FSK leaves 41% idle and FFSK
      51%. That is about twice the study's estimate, so it doesn't yet leave room for the sotto
      decoder (55-63%).
    - Buys: the live 4FSK voice pager.
    - Profiled (2026-11-06): the filter took 21%, counting and polling about 21%, the template
      sums 9%. A mirrored ring, packed taps and moving the key check off the receiver's bursts
      took idle to 52% (4FSK) and 63% (FFSK); docs/k6-if-modem.md has the table.
    - Settled by: the bench's `i=` numbers. The decoder still wants about 10 more points, from
      the counting (an unrolled loop, about 6% in `gp0dma`'s estimate).
16. **Take a preamble's timing at the leading edge of its fit, not at its best.**
    - From: `iflive` in the emulator (2026-11-06). The 4FSK lead's fit against the clock offset
      is a plateau a third of a symbol wide. Its best fell anywhere on it, and on the late side
      the timing loop held a side peak (0.26 symbol late, 8% BER all step). Walking back from
      the best while the fit stays over half of it found the true timing in every run.
    - Buys: false locks fell from 4-6 in 18 streams to none, for about 70 B of code. The same applies to any
      module that sweeps a clock against a known preamble (FT8's Costas arrays, CW's dits).
    - Settled: yes, on the emulator; the first `iflive` bench run checks it on air.
17. **The K6 transmitting FSK the MCU writes.**
    - From: the "Transmit our own FSK from the K6" thread, 2026-11-06 ([k6-own-fsk.md](k6-own-fsk.md)).
    - Tested on air (xtalstep, PR #71): the crystal value REG_3B and the frequency word REG_38 are
      latched. Neither moves the synthesizer until RXON/TXON, so there is no per-symbol modulator
      there. After RXON, REG_3B steps exactly 84.8 Hz at 440.850 MHz.
    - Untested: the tone oscillator (REG_70/71) as continuous-phase audio FSK, with the frequency
      word changed per symbol. It would give 4-tone audio FSK, matching the receive thread's 4FSK
      tones (900/1500/2100/2700 Hz).
    - Buys: K6-to-PC and K6-to-K6 links in modes the chip's FSK modem lacks. The module TX API
      (level 3, PR #71) is the prerequisite; it isn't on the radio yet.
    - Settled by: a module that switches REG_71 per symbol while keyed, recorded on the IC-7100.
18. **FT8 (and WSPR) from the receive IF.**
    - From: the FT8/WSPR thread, 2026-11-06 ([k6-ft8-wspr.md](k6-ft8-wspr.md)).
    - Tested in simulation and on the emulated M0: `ft8wf` (3.8 KB) streams a 4-bit waterfall
      that the PC decodes; 75% at -16 dB through the modelled GP0 path. The whole decoder
      `ft8rx` is 7.2 KB and waits for a bigger module arena.
    - Run 1 on air (2026-11-06): the IF offset read +11 Hz (with the AFC on; see run 5). The
      noise rms reached only 4.1 at the top gain step (the sim assumed 8), so expect the sim's
      floor-0.06 numbers (-16 dB 42%). No decodes: the ring overran (see 19).
    - Run 2: no overruns, but no tones in the window. Run 3's raw-IF capture showed why: the
      sideband is upright, but the chip's AFC pulls a USB tone toward the IF's centre (time
      constant about 30 s). REG_73 bit 4 alone didn't stop it; ft8wf now retunes (RXON) after
      setting it, as REG_38/3B need.
    - Run 4: the first decode (1 of 7). Run 5 (AFC off, then RXON) showed the true offset:
      the carrier is 145 Hz low (run 1's +11 Hz was the AFC's doing). Aimed at `--f0 1624`, the
      K6 decoded 6 of 7 frames at s=-16, the three 14 dB-down frames included. FT8 receive on
      the K6 works; the miss came 3 s after a steady tone, while levels recovered.
    - Drift (K6 and IC-7100 together): 0.05-0.17 Hz/s on the bench. Ample for FT8; for WSPR
      it means 5-20 Hz over a frame, marginal.
    - Untested: drift over temperature and over 10 minutes, which decides WSPR at 440 MHz;
      `ft8rx` on the radio (needs the arena).
    - Buys: a weak-signal mode on the pager's own hardware, and a first user of a bigger arena.
    - Settled by: a 10-minute IF log of a bare carrier from a cold start; the arena change.
19. **Keep API calls out of a streaming module's hot loop.**
    - From: the FT8/WSPR thread, run 1 (2026-11-06).
    - `api->key_down()` costs about 60 us on the radio: Aura's `any_pressed` scans six keypad
      rows with a 5 us SysTick wait each. Called on every drain pass, it overran ft8wf's 93 us
      ring about 6,450 times a second. `tests/k6_live_emu.py` charges 1 us for it; at 60 us the
      emulator reproduces the radio's overrun rate (6,140 a second).
    - Buys: emulator runs that predict the radio, and a rule for module authors: check the key
      once per row, block or line, never per DMA poll.
    - Settled by: run 2 of ft8wf (key checked once a row) showing `lost=0` on the radio; then
      cost key_down at 60 us in the emulator.
20. **The tone oscillator is a module's transmitter (no firmware change).**
    - From: the FT8/WSPR thread, bench run 6 (2026-11-06).
    - Tested on air: with `tx_on`'s REG_30 = 0xC1FA (no AF DAC bit, unlike Aura's 0xC3FA), a
      module's REG_70 = 0x8000 | gain << 8 and REG_71 = f x 10.32444 go out as FM audio. ft8tx
      stepped REG_71 every 5 ms for 12.64 s and the IC-7100 decoded FT8 at snr -14; the MCW ID
      (REG_70 keyed) came through clean. Tone gain 60 gave about -28 dBFS of tone on the
      IC-7100's USB audio.
    - `tx_on` blocks about 1.4 s (Aura's `enter_tx`); time a transmission from its return.
    - Buys: any audio-FSK mode (AFSK, 4FSK, MFSK, slow-scan) from a level 3 module, at
      0.097 Hz resolution and 5 ms steps or finer. Insight 17's FSK question is answered for
      audio FSK.
    - Settled by: run 6; a gain sweep for deviation is still to do.
21. **K6-to-K6 paging (parked, Jeff 2026-11-06).**
    - From: the upload ARQ thread. The ARQ reports are the K6 sending a pager packet with its
      own chip FSK, so one K6 could page another directly: no PC or IC-7100 in the path.
    - Buys: a two-radio text pager, and a relay or store-and-forward test bed.
    - Parked by Jeff ("definitely will want that later"). Settled by: the ARQ reports decoding
      on another K6, not just the IC-7100; then a send-text module on a key.
22. **gp0dma's IF reading was biased high.**
    - From: the ifcal thread (2026-11-06), in the emulator. `run()` scaled the IF by
      `cyc >> 10`, floored, so it read up to 1/1000 high: about 17 Hz at BLK 940, up to 3.4 Hz at
      the sweep's 4688. It now rounds and scales with 8x the time resolution; ifcal reads a
      -145.5 Hz offset as -145.1 in the model.
    - Buys: an IF offset good to about half a Hz, which FT8 (6.25 Hz tones) and WSPR (1.46 Hz)
      both need. Earlier `if=` readings from gp0dma and gp0sweep are a few Hz high.
    - Settled by: ifcal on the radio against `tools/k6_ft8.py tone`'s -145 Hz.
23. **The ARQ report's PHY is untested from the K6.**
    - The chip's FSK has sent calibration bursts at 1200 baud; the reports go at the pager's
      F/1800. If the IC-7100 doesn't decode them, try the reports at 1200 (a REG_72 write
      around the send) before anything else.
    - Settled by: the first `tools/k6_upload.py` run on the bench.
24. **Time everything on the K6's DMA by the clock, not by assumption.**
    - From: the first live-receiver run (2026-11-06). Channel 1's free-running rate was 5490
      kS/s under gp0dma, 5555 under gp0cap, and 5875 while iflive only polled. It moves with
      the CPU's bus traffic, and a module that times it in one loop and decodes in another is
      off by up to 7%.
    - Buys: a receiver that acquires (it needs the rate to 0.05%) and a ring index that
      survives a stall. Any other DMA module (gp0dma, gp0cap, ft8rx, ft8wf) that measures its
      rate once has the same exposure.
    - Settled (2026-11-07, 01:56 UTC rerun): `k=` read 5566 on every step of both modes,
      5563-5564 on steps with no lock, though the CPU sat 60% idle in 4FSK and 76% in FFSK. So
      while decoding, the rate is steady and barely depends on the load; 5875 was a polling-only
      loop. Measuring it as it runs is still what made the receiver lock.
25. **The REG_63 noise indicator as a sensitivity meter.**
    - From: the coverage thread (2026-11-06, [k6-rxmap.md](k6-rxmap.md)). Phase 0 read 0x0000
      on a strong carrier and 0x002a on an empty channel. If REG_63 rises smoothly as a carrier
      weakens, it measures FM quieting, which is close to what SINAD measures. Then sensitivity
      could be read off the IC-7100's power steps without a calibrated generator.
    - Buys: a sensitivity number per band, and a squelch that opens on quieting, not on RSSI.
    - Settled by: `k6_rxmap.py cal`'s `z` column against its levels (power steps at 440.850,
      on a dummy load).
26. **RSSI under-reads after a short tune, and by more for weak signals?**
    - Phase 0's curves come from a -20 dBm carrier. rxmap's quick pass (3 ms) only ranks the
      channels in a bin, then re-reads the winner at 30 ms. If a weak signal (-110 dBm) ramps
      slower than the empty channel next to it, the quick pass ranks it below the noise.
    - Settled by: a map run with `--quick-us 12000` over the same segment, compared bin by bin.
27. **Where does the K6's extra detector noise come from?**
    - From: the softmodem thread (2026-11-07). On the gp0cap captures the timing and AFC
      detectors are 3-10 times noisier than on the model at 70 dB-Hz (their variance 0.13-0.57
      against 0.004-0.015), while the decisions stay clean (FFSK no errors down to -72 dBm).
      The noise is white from one symbol to the next and doesn't depend on the symbol pattern.
      FM noise added on the model (to 200 Hz rms) moves the AFC detector but hardly the timing
      one, so it isn't simply LO phase noise. Candidates: the sigma-delta's real behaviour on
      code 13, sample-time jitter in the DMA, or the IC-7100's audio chain.
    - Buys: whatever it is caps 4FSK. Narrower loops (iflive, 2026-11-07) only shrink its
      effect, and the detectors' biases on real signals (timing -0.3 half-samples, AFC +15 Hz
      from the best) may be the same thing.
    - Settled by: a gp0cap pass on an unmodulated carrier (the IC-7100's FM-D with no audio):
      the turn between half symbols should be flat. If it's as noisy, it's the K6's IF chain,
      not the sender.
28. **Schedule work between the IF receiver's bursts, not after them.**
    - From: the coded 4FSK thread, 2026-11-07 ([k6-if-fec.md](k6-if-fec.md)). Tested on the
      emulator only.
    - The ifrx receiver works in two bursts a symbol (half way through it, and at its end), about
      120 us each, against a DMA ring that lasts 183 us. Any other job begun just before a burst
      overruns the ring, even a 30 us one. In the coded session, a Viterbi step ran whenever the
      loop had caught up, and lost about 600 counts in 10 s. Run only at sample indexes 1-5 and
      10-14 of the symbol, it lost none, with the same idle time. The packet check runs at 1-4.
    - It would buy: any module or image that adds work to the receiver (a decoder, a CW ID
      check, a screen update) can do so without a bigger ring. The rule is "start a job only
      where it ends before the next burst".
    - Settled by: a bench session's `lost=` (it should be 0 on the K6 as in the emulator), and
      the same rule tried on iflive's own key checks.
29. **Copy a hot loop into RAM inside a flash image.**
    - From: the coded 4FSK thread, 2026-11-07 ([k6-if-fec.md](k6-if-fec.md)). Tested on the
      emulator only.
    - The image runs from flash at two wait states, about 1.33x slower than a RAM module (23%
      idle against 60% in the same loop). ifpager puts its four hot functions in one section,
      `.text.ifrx_ram` (390 B), copies it onto the stack keeping each word's alignment, and calls
      the copies through pointers. Tables are reached only through a struct pointer, so the block
      has no relocations and works both in the image and under `-fropi`. A test checks for
      relocations and the section's order.
    - It would buy: any image feature with a tight inner loop (a decoder, an FFT, a filter) gets
      module speed without a RAM module. With a section of the RAM region instead of the stack,
      it costs no stack at all.
    - Settled by: PR #108's on-air `ram=` and `idle=` against the emulator's.
30. **Hear HF below the FD6818's 18 MHz spec.**
    - From: the HF thread, 2026-11-07 ([k6-hf.md](k6-hf.md)). Untested on the radio.
    - 18 MHz was where the coverage run started, not a measured limit. Aura's tune path has no clamp,
      and the low end is the VCO's bottom over its largest divider. UV-K5 forks claim 14-15 MHz, which
      would reach 20 m. `rxmap` segments `hf` and `hflo` scan it with the module already on the radio.
      `hfprobe` tries the front-end path pins (PA13/PA14) and REG_3E/REG_1F at exact-frequency
      markers (clock harmonics, WWV, CHU).
    - It would buy: HF listening with no hardware change. 20 m FT8 through ft8wf's GP0 software
      demodulator would be the showpiece.
    - Settled by: the clock birdies at 12 and 13 MHz turning up at their own frequency, or not.
31. **Let the PC stop a running module.**
    - From: the WSPR thread, 2026-11-07 ([k6-ft8-wspr.md](k6-ft8-wspr.md)). Not built.
    - On the bench the WSPR beacon (`wsprbcn`, 6 frames over 50 minutes) could only be stopped
      by a key press on the radio, because the pager reads no control packets while a module
      runs. The bench was held for the rest of the hour. One option is a firmware `stop` flag the
      pager's receive path sets on a control packet, which modules poll with their key check
      (`api->stop()`, next to `key_down`). Another needs no firmware: a module that waits on RSSI
      (as `wsprbcn` does between frames) stops on a two-carrier pattern that a CW ID can't make,
      for example two 1 s carriers 2 s apart.
    - It would buy: long-running modules (beacons, scanners, loggers) on an unattended bench.
    - Settled by: `k6_bench.py stop` ending `wsprbcn` between frames without anyone at the radio.
32. **A sync hunt on outer tones must also see modulation.**
    - From: the PHY G thread, 2026-11-07 ([k6-if-fec.md](k6-if-fec.md)). Settled on the model
      and on air (PR #120).
    - On the half-baud tone grid an unmodulated carrier correlates well with both outer tones
      (600 Hz is a half turn a symbol, 2400 Hz two turns), so the hard decisions in the carrier
      before a lead and after a burst are runs of 0 and 3 with few inner tones, and a sync hunt
      that counts only inner tones as wrong found a sync word 78 symbols after a burst's last
      packet. The hunt now also counts an outer tone as wrong when the other outer tone's metric
      is within 3/4 of the winner's, which on a sent tone happens 5% of the time at the floor.
    - It would buy: the same check for any future tone plan or sync word; a bare-carrier
      detector for free (both outer metrics high, no inner) if a module ever wants one.
    - Settled by: 56 packets on air with no false sync, and the model at 45-55 dB-Hz.
33. **Rate 5/6 on the 600-2400 Hz tones.**
    - From: the PHY G thread, 2026-11-07. Not built.
    - The new tones made 0 raw errors in 80,400 bits on air and rate 3/4 decodes 13-14 of 14.
      The K=7 code's rate 5/6 pattern (133: 1 0 1 0 1 / 171: 1 1 0 1 0, 6 bits each 5 steps)
      would give 2064 x 3/5 = 1238 coded bits a packet, not a whole number of 86-bit blocks, so
      it needs a 14-block padding rule or a block size of 1238/2 = 619 (prime: no). The nearest
      clean fit is 6 bits each 5 steps over 1032 steps = 206.4 groups, so the tail would need
      8 steps' worth of extra zeros (1040 steps, 1248 bits, still not a multiple of 86) or a
      block of 78 bits (16 blocks of 78 = 1248). About 2000 bit/s net, 1.3x F/1800.
    - It would buy: the first K6 link clearly faster than the chip's modem, with soft decoding.
    - Settled by: the model at about 51 dB-Hz for every packet, and a bench run as for rate 3/4.
34. **Build Aura's `App` in its static instead of on the boot stack.**
    - From: the hiss band thread, 2026-11-07 ([k6-hiss-port.md](k6-hiss-port.md)). Not built.
    - `place_app` (firmware/k6-aura, main.rs) builds `App::new`'s 3 KB result in its own stack
      frame and copies it into `APP_SLOT`: 3448 B of stack at boot, on top of main's 2952, that
      nothing else ever uses. Writing the result through the slot's raw pointer changed nothing
      (LLVM kept the temporary), so it would take `App::new` writing its fields into a
      `MaybeUninit<App>` one by one. The hiss band image first got its RAM by cutting its
      message store to 2.5 KB; then the 2720 B above Aura's old region turned out to be real
      SRAM (`ramprobe`, 2026-11-07) and `memory.x` grew, so this is less pressing.
    - It would buy: about 3.4 KB of boot-time stack in every image, so the stores could grow
      back and a 16 kHz ring of four segments would fit.
    - Settled by: `tools/k6_frames.py` showing `place_app` under 500 B and the image booting.
35. **Read DMR's sync words off the GP0 IF.**
    - From: the DMR thread, 2026-11-08 ([k6-dmr.md](k6-dmr.md)). Modelled and emulated, not yet on air.
    - DMR's 48-bit syncs use only the +-1944 Hz symbols and are balanced, so a normalised
      correlation on the zero-crossing discriminator, at 2 points a symbol, finds them at
      rho 0.95-0.99 where FM voice, FFSK and tones stay under 0.77. Two hits a whole number of
      30 ms slots apart confirm DMR. `dmrdet` mutes the speaker on it (3 KB code, ~45% CPU
      while a DMR carrier is up). A repeater's output doesn't pulse (both slots go out), so
      RSSI alone can't do it.
    - It would buy: a K6 that mutes DMR (and later YSF/P25) buzz, an EXPLORE "DMR" mark, and a
      way into DMR metadata (colour code, talkgroup, source ID) with the same front end.
    - Settled by: `mod-dmrdet.bin` on 440.925 with the IC-7100 recording alongside.
36. **Share one RAM block between the voice path and the modules.**
    - From: the memory plan thread, 2026-11-08 (Claude Doc "K6 memory plan: RAM, modules and
      image switching"). Built (this section's "How much room"); not yet on air.
    - The message store, the PWM ring and the decoder's pool are only busy while a message is
      received or played, and a module never runs then, so the three sit side by side in
      `hal::arena` and a module gets all 9,760 bytes instead of the 4 KB store (864 of them from shrinking two arrays in `App` that only stock Aura screens use: insight 38, half done). The 8 kHz images
      pay about 2 KB of the stack they had spare, so every SPI image offers the same block as
      the hiss image.
    - It would buy: modules up to 9.5 KB of code and scratch, most of the way to `ft8rx`
      (7.2 KB of code and about 4 KB of RAM; without `fk_text`, 0.9 KB, it needs about 10.3 KB).
    - Settled by: `K6 ARENA` at boot, a module over 4 KB running, and voice playback with a
      matching `pcm=` and a sane `stack_free=` on a v3.7 and a hiss image.
37. **Draw the screen a page at a time.**
    - From: the memory plan thread, 2026-11-08 (Jeff: page the screen buffer out). Not built.
    - Aura keeps a whole 1 KB copy of the 128x64 screen in main's stack frame for good, though
      the ST7565 holds its own copy. u8g2's page-buffer mode draws a frame once per 128-byte
      page and sends each page as it goes: 128 B of RAM instead of 1 KB, for 8 passes of the
      drawing code (mostly text here). Redrawing only the pages that changed is cheaper still.
    - It would buy: about 896 B more for the module block in every image.
    - Settled by: the screens looking the same, a redraw time on the serial log, and the
      `boot stack:` and `K6 ARENA` lines showing the bytes.
38. **Drop stock Aura's screen state from `App` in the pager images.**
    - From: the memory plan thread, 2026-11-08. Half built: `MAX_SATELLITES` 1 and the spectrum's
      `MAX_BINS` 1 under `pager-only` took `App` from 3,028 to 2,164 B, and `place_app`'s boot
      frame from 3,448 to 2,296 B; the 864 B went into the shared block (insight 36).
    - Left: SSTV 204, settings 156, the satellite screen 152, the channel manager 132 B, which
      would take `cfg` on code the pager images never reach rather than one constant each.
    - It would buy: about 0.6 KB more for the module block.
    - Settled by: `tools/k6_frames.py --sizes` showing `App` under 1.6 KB and the image booting.
