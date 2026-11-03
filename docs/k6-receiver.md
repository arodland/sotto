# K6 voice pager receiver: sotto messages on a stock Baofeng UV-K6

*2026-11-03.* The Baofeng UV-K6 can't encode sotto, and its mic never reaches the MCU (see
`docs/ht-survey.md`). It can **receive a sotto message as data, keep it in RAM, and play it**, with
no hardware change and no flash write per message. This page covers that receive side: sotto's C
library (`c/k6/`, MIT) and the Aura firmware patch (`firmware/k6-aura/`, GPL-3.0 like Aura). The
packet format is the shared contract `k6-fsk-packet-spec.md`, version 1. Its reference packer is
[`c/k6/k6fsk_ref.py`](../c/k6/k6fsk_ref.py). The transmitter is the IC-7100 softmodem,
[`docs/k6-pager.md`](k6-pager.md).

```
PC: c/sotto_enc (v3.7)  ->  k6fsk packets  ->  IC-7100 FSK  ~~~>  K6 FD6818 FSK modem (FIFO)
  -> sp_packet: de-whiten, CRC, frames into a RAM store with good flags (c/k6/sotto_pager.c)
  -> sp_play_pwm: fixed-point M0 decoder (FX_NARROW_MUL, fx_oa_block) -> pre-emphasis, gain
  -> DMA ring -> TIM1 CH4 PWM on PA11 -> RC filter -> speaker amplifier (src/hal/pwm_audio.rs)
```

## What runs where

- **`c/k6/sotto_pager.c`** handles the packets and playback. It de-whitens with PN9, checks the
  magic and CRC-16, and puts frames in their place per the spec's interleave (packet k of group g
  carries frames g*64 + 8s + k). It keeps a good bit per frame and per packet, so repeats fill
  holes. A new msg_id drops the old message. It also handles profile S, the 16-byte fallback.
  Playback decodes frame by frame and conceals missing frames. There is no heap: the store is in
  the caller's buffer, and the decoder's one `calloc` is a static 864-byte block.
- **`tools/k6_build.sh`** builds `libsotto_k6.a` for thumbv6m with clang, plus a bench ELF. The
  library leaves `memcpy`/`memset` and the ARM run-time helpers to Rust's compiler_builtins.
- **Aura patch** (`firmware/k6-aura/`) contains three files:
  - `app/pager.rs`: the FSK registers (spec section 9), the receive loop, keys, playback and the
    calibration transmit.
  - `hal/pwm_audio.rs`: the stock voice prompts' PWM setup. TIM1 counts to 2000 at 96 MHz (48 kHz),
    RCR = 5 gives 8 kHz updates, and DMA1 channel 5 feeds TIM1_DMAR from a 4 x 160 ring. It polls
    the DMA counter, with no interrupt.
  - `ui/pager.rs`: the screen.

## Measured (Cortex-M0 emulator, KD32F328 at 96 MHz, two flash wait states)

`python c/k6/bench_k6.py c/k6/build/bench.elf CLIP` runs the radio's call sequence (`sp_init`,
`sp_packet` per packet, `sp_play_start`, `sp_play_pwm` per frame) on the emulated M0. The timing
model is in `c/fixed/cm0/run.py`. Results are in `docs/results/k6/`.

| Clip (v3.7, all packets) | Decode + PWM, MHz mean / worst frame | ms per 20 ms frame |
|---|---:|---:|
| ve9qrp 10 s | 60.0 / 74.3 | 12.5 / 15.5 |
| david4 10 s | 57.7 / 74.1 | 12.0 / 15.4 |
| vk2tpm 10 s | 61.0 / 88.5 | 12.7 / 18.4 |
| kristoff 5 s | 52.6 / 73.5 | 11.0 / 15.3 |
| ve9qrp, 5 of 64 packets lost | 59.9 / 84.9 | 12.5 / 17.7 |

- **Packets** take 9,000-10,000 cycles each (0.1 ms). A 64-byte packet lasts 560 ms on air at
  1200 baud.
- **Stack** is 3.5 KB at the deepest decode.
- **Static RAM in the library** is 3.4 KB: 0.8 KB of decoder state, the 2 KB packed cos/sin table,
  and buffers. Building with `-DFX_OA_UNPACKED` saves the 2 KB, but costs 68-69 MHz mean and 86-104
  worst.
- **Flash** is 62 KB: 30 KB codebook, 8 KB minimum-phase tables, 20 KB code.
- **Output.** The M0's PWM values equal `c/k6/pager_host -p` exactly, and the host's PCM equals
  `sotto_dec_fx_m0`'s (`tests/test_k6_pager.py`).

Playback stays ahead: the worst frame (18.4 ms) is under its 20 ms. The 80 ms ring is pre-filled
before the amplifier opens, so a frame or two slower than the model still doesn't run dry. The
timing model is ±25%. On the radio, the VOICE MSG screen shows the decoder's real time per frame
after each playback (TIM14, 100 µs ticks), which checks the model.

## The audio path

The PWM pin goes through 4.7 kΩ / 3.9 nF, then 4.7 kΩ / 22 nF, then 22 kΩ and 1 µF into the node
the RF chip's AF output also feeds through 2.2 kΩ (schematic BF_K6_Plus_Open_V1.1). On paper that is
-3 dB at 1 kHz, -7 dB at 2 kHz and -10 dB at 3 kHz: the stock prompts are muffled. `sp_pcm_to_pwm`
optionally applies `(33 x[n] - 21 x[n-1] + 4 x[n-2]) / 16`. That is flat at DC and +11 dB at 4 kHz,
and it flattens the computed response to ±0.5 dB up to 3.5 kHz, assuming the 2.2 kΩ node is the
load. It is not measured. Key 4 on the radio turns it off. The RF chip's audio is muted and the
amplifier is enabled (PB2) only during playback.

Senders' levels vary by 15 dB or more, so `sp_auto_gain_q8` sets the gain from the loudest frame's
5-bit energy field, putting it at -13 dBFS within -6..+18 dB. On six clips that clips at most 0.025%
of samples after the pre-emphasis. UP and DOWN add ±3 dB steps.

## RAM budget (Aura's application gets 17,720 bytes)

| | Bytes |
|---|---:|
| Message store (`STORE_BYTES`, 640 frames = 12.8 s) | 4,096 |
| PWM DMA ring, 4 x 160 | 1,280 |
| sotto library static (decoder state, cos/sin table, buffers) | 3,432 |
| Decoder stack at its deepest, on top of Aura's main-loop stack | 3,500 |
| **Pager total** | **~12,300** |

Aura keeps almost everything on the main stack (the `App` struct, the 1 KB LCD page buffer), so its
own use isn't known until it is built and run. `firmware/k6-aura/build.sh` reports static RAM, and
the VOICE MSG screen reports how much RAM the stack never reached. That figure comes from painting
the free RAM on entering the app and scanning it after a playback. If it is tight, the first knobs
are `STORE_BYTES` (3.2 s per KB) and `-DFX_OA_UNPACKED` (2 KB).

**All of Aura plus the pager doesn't fit; the pager-only image does.** Measured by the *K6 pager firmware* CI job (`firmware/k6-aura/build.sh`):

| | Flash, bytes | Static RAM, bytes |
|---|---:|---:|
| Stock Aura `edf3b98` | 115,208 (7,672 free of 122,880) | 340 |
| Aura + pager | 180,788 (**57,908 over**) | 9,144 |
| Pager-only image (`--features pager-only`) | 102,228 (20,652 free) | 9,144 (8,576 left for the stack) |

Aura fills 94% of the application flash by itself, and the pager adds 65.6 KB: the 30 KB
codebook, 8 KB of minimum-phase tables, about 5 KB of other tables, and the decoder and packet
code. Freeing 58 KB takes one of these:

- **A pager-only image built from Aura** (done: the patch's `pager-only` feature, built by
  `build.sh` as `out/aura-pager-only.bin`). It has Aura's drivers, its saved settings and VFO, a frequency entry (#, RAM only), and
  VOICE MSG, without the rest of Aura's apps and menus. To use the radio normally again, you flash stock Aura back over it.
- **Keep all of Aura and move the codebook to the SPI flash**: 30 KB, written once at install, like
  Aura's SSTV image import. That still leaves about 28 KB to cut from Aura and from the library
  (for example `-Os`, and dropping apps), and nothing measured yet shows where those bytes would
  come from.

## What is not verified yet

- The firmware is built only in CI (*K6 pager firmware*). This sandbox won't run Aura's cargo
  build, so the Rust was type-checked here against stubs and compiled for real in CI.
- FD6818 FSK receive has never been shown working by anyone. Aura's FSK functions are dead code.
  The register sequence follows the spec, from the BK4819 register list and egzumer's 72-byte
  AIRCOPY. The screen shows sync polarity, CRC, magic byte and timeouts for the first test.
- PWM playback with this DMA setup follows the stock firmware's `AudioHard_Init` register for
  register, but Aura never drives PA11, so it is untested under Aura.
- The cycle counts come from a model (±25%), not the chip.

## Files

- `c/k6/sotto_pager.[ch]`: the library API (`sp_init`, `sp_packet`, `sp_status`, `sp_play_start`,
  `sp_play_pwm`, `sp_auto_gain_q8`, `sp_test_packet`).
- `c/k6/pager_host.c`: the same path on x86 (`make -C c/k6`). `-t` prints the calibration packet,
  and `-p` writes PWM values.
- `c/k6/bench_k6.py`: the M0 cycle bench.
- `c/k6/k6fsk_ref.py`: a copy of the spec's reference packer.
- `tests/test_k6_pager.py`: spec test vector, loss, repeats, new message, truncation, profile S,
  automatic gain and PWM values.
- `firmware/k6-aura/`: the Aura patch, build script, and flashing guide.
