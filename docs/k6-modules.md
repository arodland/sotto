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
3. The K6 (`c/k6/k6_mod.c`) puts the chunks straight into the 4 KB message store, in any
   order, repeats ignored. Once all are in, it checks the header, the tag and the serial,
   then calls the entry function with a table of services. The store is set up for messages
   again afterwards, so any message held before the module is gone.

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
| 0 | `0xAB` |
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
| 12 | API level the module needs (1 or 2; a K6 refuses a newer one), flags (0), 2 reserved bytes |
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
| `scratch(&bytes)` | RAM free while the module runs: the rest of the 4 KB module store past the code, word aligned, its size in `bytes` |

Level 2 is receive only: there is no way to key the transmitter. After the module returns the
firmware puts the radio back on the pager's frequency and sets the FSK receiver up again,
whatever the module changed. `KM_RSSI_DBM(fd_read(0x67))` is the RSSI in dBm. A module that
uses level 2 says so with `#define K6_MODULE_LEVEL 2` before including `k6_mod.h`;
`modem.k6 mod` reads it and puts it in the header (`--api-level` for a `.bin`).

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
  keep it small: the module runs on the pager's stack, with about 700 B to spare. Level 2's
  `scratch()` gives the rest of the module store for tables and buffers (4 KB less the
  code). `K6 MOD run` reports the stack room at the call (`stack=`) and the scratch size,
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
    - Settled by: the first `iflive` bench run (every step should show `s=2`).
15. **The IF receiver's CPU.**
    - From: `iflive` on the emulator at 1.5 cycles an instruction: 4FSK leaves 41% idle and FFSK
      51%. That is about twice the study's estimate, so it doesn't yet leave room for the sotto
      decoder (55-63%).
    - Buys: the live 4FSK voice pager.
    - Settled by: a per-function profile on the emulator, then the bench's `i=` numbers.
      Suspects are the per-count loop overhead and the 24-tap filter at 20 samples a symbol.
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
