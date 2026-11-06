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
| `fd_write(reg, v)` | writes one, 0; or -1 for the registers it refuses: 0x30 (RX/TX state), 0x33 (GPIO), 0x36 (PA), 0x37 (power) |
| `delay_us(us)` | busy wait, any length |
| `tune(hz, wide)` | receive on `hz`: band switch, PLL, wide or narrow filter, RX on |
| `rx_hz()` | the frequency the pager listens on |
| `ms()` | ms since the module started, 32 bits |
| `key_down()` | 1 while any key is held |

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
K6 MOD run serial=... code=366 B at 0x2000....
K6 MOD say hello world from RAM at 0x2000....
K6 MOD say sum of squares 0x9f31fc58 in 0x.... ticks of 100 us
K6 MOD ret=107 (0x6b) in .. ms
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
  keep it small: the module runs on the pager's stack, about 1 KB deep at most.
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
