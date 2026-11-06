# sotto's codebook in the K6's SPI flash

The K6 pager's decoder needs sotto's shape codebook: 4 stages of 512, 512, 256 and 256 vectors,
20 bytes each, 30,720 bytes in all. That is a quarter of the KD32F328's 120 KB application flash.
Each frame uses only one vector per stage, so the K6 doesn't need the table in the MCU's flash.
It can read each vector from the radio's 2 MB SPI flash when the decoder asks for it.
A v3.7 frame takes 4 reads per 20 ms, and a sotto 1200 slot takes 3 reads per 40 ms.

The bytes are the table's, so the decode is bit-exact with the table build, and the pager image
gets about 30 KB of MCU flash back. v3.7 and sotto 1200 share the codebook, so one image in SPI
flash serves both.

## How it works

- **The hook (`c/fixed/fx_tables.h`).** The decoder gets each vector through
  `FX_CB_VEC(stage, index, buf)`. In a normal build that is `fx_cb[stage] + index * FX_CBW`, so
  nothing changes. With `-DFX_CB_SPI` it calls `fx_cb_fetch`, which reads the 20 bytes into
  `buf`. The decoder no longer references `fx_cb`, so `--gc-sections` drops the table.
- **The module (`c/k6/sp_cb.h`, `sp_cb.c`).** This defines the image format, the check, the
  one-time install, `fx_cb_fetch` and a timing bench. The firmware supplies three functions:
  `sp_flash_read`, `sp_flash_erase4k` and `sp_flash_write`. Aura's are in the patch's
  `src/app/pager.rs`, on its `NorFlash` driver.
- **The image** sits at SPI flash 0x100000 (32 + 30,720 bytes, 8 sectors of 4 KB). That is above
  everything Aura uses, which ends near 0xED200, and in the first slots of the module library
  planned in the explorer design. The image is a 32-byte header followed by the table:
  - the magic `K6CB`, then version 1
  - 20 bytes per vector and the table's length
  - the stages and their bits (9 9 8 8)
  - the table's CRC-32, then the header's CRC-32 (both zlib's CRC-32)

  `c/k6/cb_image OUT.bin` writes the image, and `cb_image -c` prints its CRC. For this
  codebook the CRC is `d5856ecd`.
- **Power-on check.** `sp_cb_setup` reads the header and compares it with the codebook the build
  expects. A build without the table gets the expected CRC at compile time (`-DSP_CB_CRC`). It
  then reads the whole table back and checks its CRC. It returns one of these:

  | result | meaning |
  |---|---|
  | 1 | the image is good |
  | 2 | the image was written now and verified (install build) |
  | -1 | no image (erased) |
  | -2 | the header is damaged, or the format is different |
  | -3 | a good image of a different codebook |
  | -4 | the table's CRC is wrong |
  | -5 | the write didn't verify |

  Until the check passes, the pager stores messages but plays none of them. The screen says
  `NO CODEBOOK (r)`. This means a wrong codebook never plays as noise.
- **The install build** (`K6_CB=install`, `-DSP_CB_INSTALL`) also links the table. If the check
  fails, it erases the 8 sectors, writes the image in 64-byte pieces that never cross a page,
  and checks it again. It writes only when the image is missing or wrong, so once the image is
  there, later boots just read it.

## Builds

```sh
make -C c/k6                       # also cb_image, pager_host_cbs, pager_host_lr_cbs, pager_host_cbi
python -m pytest tests/test_k6_cb.py
K6_CB=spi sh tools/k6_build.sh     # c/k6/build-cbs: library without the table, + sotto-cb.bin
K6_CB=install sh tools/k6_build.sh # c/k6/build-cbi: with the table and the installer
K6_CODEC=1200 K6_CB=spi sh tools/k6_build.sh   # c/k6/build-lr-cbs
python c/k6/bench_k6.py c/k6/build-cbs/bench.elf clip.wav   # bench.elf serves the image from MCU flash
sh firmware/k6-aura/build.sh       # out/aura-pager-only.bin, aura-pager-lr.bin, aura-pager-cbi.bin, sotto-cb.bin,
                                   # + aura-pager-only-table.bin, aura-pager-lr-table.bin (TABLE=0 skips them)
```

Since 2026-11-06 the images `build.sh` writes as `aura-pager-only` (v3.7) and `aura-pager-lr`
(sotto 1200) are the ones without the table. They were `aura-pager-cbs` and
`aura-pager-lr-cbs` before that. The table builds are now `aura-pager-only-table` and
`aura-pager-lr-table`. A radio needs `aura-pager-cbi` once, to write the codebook.

The host harnesses take `-F IMAGE`, the image to put at 0x100000 in the emulated flash, and
`-W FILE`, the image region after setup. They print `cb=` and `mode=`, and `cb_reads=` after
`pcm=`. `pager_host_cbs -B 1000 -F out/sotto-cb.bin` prints the sum the radio's bench line
should show. The tests check these things:

- The SPI builds play the same PCM and `pcm=` hash as the table builds. This covers v3.7, sotto
  1200, packets lost and streaming.
- The read counts are 4 per v3.7 frame and 3 per sotto 1200 slot.
- Each kind of bad image plays nothing.
- The install writes `cb_image`'s image byte for byte, and only when it has to.

## Measured

**M0 emulator** (`bench_k6.py`, 96 MHz, two flash wait states, the 10 s david4 clip, decode plus
PWM per 20 ms). The PCM is identical to the table build's. The bench serves the image from MCU
flash, so the time spent on the SPI wire isn't in these numbers.

| build | mean MHz | worst MHz |
|---|---|---|
| v3.7, table | 59.7 | 76.1 |
| v3.7, SPI | 59.9 | 76.4 |
| sotto 1200, table | 59.9 | 79.1 |
| sotto 1200, SPI | 60.0 | 79.4 |

**SPI time, estimated.** One read sends 24 bytes over SPI1: the command, 3 address bytes and 20
data bytes. Aura's driver polls one byte at a time. At 1 to 2 us a byte, a read is about 25 to 50
us. That adds about 0.1 to 0.2 ms to a 20 ms v3.7 frame, and less in sotto 1200. The radio's
bench line measures the real figure.

**Images** (pager-only, `firmware/k6-aura/build.sh`):

| image | bytes | free of 122,880 |
|---|---|---|
| aura-pager-only-table (v3.7, table) | 121,356 | 1,524 |
| aura-pager-lr-table (sotto 1200, table) | 122,304 | 576 |
| **aura-pager-cbi** (v3.7, table + installer) | 122,724 | 156 |
| **aura-pager-only** (v3.7, no table; was -cbs) | 91,372 | 31,508 |
| **aura-pager-lr** (sotto 1200, no table; was -lr-cbs) | 92,308 | 30,572 |

sotto 1200 with both the table and the installer is 780 bytes too big. That is why the
install image is v3.7. Since the codebook is the same, the image it writes serves the sotto 1200
image too.

## On the radio

The serial report prints two lines at power-on, once the pager starts:

```
K6 CB r=2 mode=2 crc=d5856ecd ms=...          install image, first boot: written and verified
K6 CB r=1 mode=2 crc=d5856ecd ms=...          every boot after that: checked only
K6 CB bench n=1000 ms=... us_per_read=... sum=9ae62243 reads=...
```

- `mode` is 0 for a table image, which touches no flash and prints `r=1 mode=0` with no bench
  line. It is 1 for a no-table image and 2 for the install image.
- `ms` is the time the setup spent, including any install.
- `sum` must be `9ae62243` for this codebook. The bench reads 1000 random vectors, and the sum
  proves they came back right.
- `us_per_read` is the SPI cost of one vector.

The test plan:

1. Key the images as usual (`modem.k6 keyimage`), then flash `aura-pager-cbi.bin` with
   `tools/k6_update.py`. It writes the SPI flash once, at 0x100000 to 0x107A20.
2. On the first boot, check for `K6 CB r=2`. Reboot (`modem.k6 ctl --action reset`, or power) and
   check for `r=1`, which shows the image wasn't written again.
3. Send a v3.7 message (`--codec 2400`) and check `tools/k6_pcmhash.py ... --check` for MATCH.
   Compare the `DEC` worst time with the table image.
4. Flash `aura-pager-lr.bin` (`aura-pager-lr-cbs.bin` before 2026-11-06) and check for
   `K6 CB r=1 mode=1`. Send sotto 1200 (VE9QRP, streamed with `--depth 4`) and check for MATCH.
   The `DEC` worst time should be within 0.5 ms of the table image's 23-24 ms.

**On air, 2026-11-06** (images from 778ebc8, F/1800 XL, the VE9QRP 10 s clip): all passed.

| image | `K6 CB` | v3.7 / sotto 1200 PLAY | DEC mean / worst |
|---|---|---|---|
| cbi, first boot | `r=2 mode=2 ms=566.4`, bench `us_per_read=34 sum=9ae62243` | | |
| cbi, after a reset | `r=1 mode=2 ms=159.9` | v3.7, 31/32 packets, MATCH | 15.85 / 23.2 ms |
| table (v3.7) | `r=1 mode=0 ms=0.0` | v3.7, 32/32, MATCH | 15.69 / 23.1 ms |
| no table, sotto 1200 | `r=1 mode=1 ms=81.6`, `us_per_read=34` | 3 sends, 14-16/16, MATCH each | 15.97-16.01 / 23.7-24.2 ms |

One read takes 34 us, so the SPI reads add 0.16 ms to a v3.7 frame's decode. In the streamed
sends, `overruns=0`, and `waits=` only counted frames from packets lost on air.

To go back, flash any table image. The codebook in SPI flash does no harm there. Aura never
reads or writes 0x100000.
