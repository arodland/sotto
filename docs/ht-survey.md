# Which handheld to run sotto on first

*2026-11-02. A survey of Yaesu handhelds, and of radios with open firmware, as targets for
sotto. Existing community projects were checked first. Part numbers come from Yaesu's
technical supplements. No firmware, decrypted or not, is part of this repository.*

## Short answer

- **No Yaesu handheld beats the FTM-300D for DSP work.** It has the fastest DSP in the
  family, a C5517 at 200 MHz. The FT-70D's C5505 runs at 120 MHz. The FTM-300D's DSP board
  also brings the DSP's full JTAG port out to its board-to-board connector.
- **The quickest way onto a real radio is OpenRTX on a TYT MD-UV380/390.** That path is well
  trodden: the firmware is open, it already runs Codec 2 over M17 through one codec file, and
  the radios are cheap. The fixed-point v3.7 encoder fits its Cortex-M4. On a cycle model it
  needs 86 MHz worst frame of 168 at -O2, plus about 15 MHz for flash wait states. The 16 kHz
  v3.8 encoder fits too, at 133 MHz worst plus about 10, built -O2
  (`docs/c-port.md`, "Fixed point on the Cortex-M4").

## Existing projects (checked first)

| Project | Radio | What it does |
|---|---|---|
| landaire/porkchop (2022) | FT-70D, then others | Decrypts the updater cipher: DES-ECB without the initial/final permutations, keyed from the build timestamp |
| MK24-0x/ft5d-flash (2026) | FT-5D/FT-5DR | Extracts, modifies, re-encrypts, repacks and flashes the MAIN (H8SX, 1 MiB) and SUB (Renesas RX, 4 MiB) images over the Renesas USB boot ROM, without Windows. The author reports over 150 test flashes. There is no image signature; a repacked updater is accepted. |
| ralfstingervip/Yaesu-FT3DR-Decompiled (2026) | FT3DR | Static analysis of MAIN + SUB. Documents the MAIN-to-DSP link (a proprietary byte protocol on a serial port, not an AMBE-3000 chip) and a DSP write mode: a key held at cold power-on turns USB into a serial bridge to the DSP. No DSP image. |
| invalidopc0de/ft70d (2022) | FT-70D | A bare-metal H8SX toolchain and blinky for the FT-70D's CPU, with the technical supplement and schematic |
| sparerib, FT-710/FTX-1 analyses, FTA-550L tools | HF rigs, aviation HT | .SFL decoders and a working decrypt, patch, re-encrypt and flash cycle. More evidence that Yaesu updates carry no signatures. |

Nobody has published custom code on a Yaesu C4FM DSP. All of the work stops at the MAIN and
SUB processors, and no DSP image is public for any model.

## Yaesu handhelds

| Radio | DSP / CPU | sotto fits? | Tools | Debug access | US price |
|---|---|---|---|---|---|
| FTM-300D (mobile, the project's radio) | C5517 200 MHz, 320 KB; MAIN H8SX/1668; SUB RX64M; AIC3204 codec | 8 kHz yes (82 / 91 MHz encoder, PR #21) | MAIN/SUB decrypted. The ft5d-flash protocol probably applies (same chips, image sizes, cipher and boot ROM; untested). The DSP image is a separate blob. | **JTAG (TRST, TMS, TDI, TDO, TCK, EMU0/1) plus DSP_BOOT, DSP_RST, FLASH_RST and a UART on the DSP unit's connector J9001** (technical supplement, DSP-1) | owned |
| FT-5DR | H8SX MAIN, RX SUB, DSP part not published | unknown | Best Yaesu tooling: an open MAIN/SUB flasher | unknown | ~$400 |
| FT3DR (discontinued) | H8SX MAIN, RX64M SUB, DSP part not published | unknown | Deep static analysis of MAIN/SUB | unknown | used |
| FT-70DR | H8SX/1668 + C5505 at 120 MHz (part suffix A12), 320 KB, AIC3204, the same 2 MB NOR flash as the FTM-300D | decoder yes, encoder no (no margin at 120 MHz) | porkchop, schematic, bare-metal demo | JTAG not routed: TMS/TDI/TDO/TCK are unconnected, and TRSTn and EMU0/1 are only tied off through 10k resistors | ~$160 |
| FT-65R / FT-4XR | no DSP: an RDA1846S transceiver chip | no | codeplug tools only | none known | ~$70-80 |

The FTM-300D and FT-70D DSP sections share one design. Both schematics carry the note
"C5505 C10=A12 / C5517 C10=VCORE", so the board takes either chip. Work on one DSP should carry
over to the other, and probably to the FT-5D's.

## Radios with open firmware (OpenRTX unless noted)

| Radio | Processor | sotto |
|---|---|---|
| TYT MD-UV380/390, MD-380, Module17, CS7000, DM-1701 | STM32F405, Cortex-M4 168 MHz, 192 KB RAM | v3.7 encoder fits (78-80 MHz mean, 85-86 worst at -O2 on the model). v3.8 at 16 kHz fits at -O2 only (116-119 mean, 133 worst; 78 KB encoder state). Build it -O2, not -Os. M17 on the TYT radios needs a small hardware mod (OpenRTX README). |
| Connect Systems CS7000-PLUS | STM32H743, Cortex-M7 480 MHz; AK2365A IF (the same IF chip as the FTM-300D) | comfortable, v3.8 too |
| LILYGO T-TWR Plus | ESP32-S3 + SA868 module | probably fits; the baseband path through the SA868 is unverified |
| Radtel RT-4D | AT32F423, 48 KB RAM | no (RAM) |
| Quansheng UV-K5/K6/5R Plus (own open firmwares, uvmod) | DP32G030, Cortex-M0 48 MHz, 64 KB flash, 16 KB RAM; BK4819 | no: even the decoder needs more than the 48 MHz (see below), and the stock 60 KB of application flash is full |
| Baofeng UV-K6 "K6 Plus Open" (Baofeng's open firmware, cnt7/BAOFENG-UV-K6-Firmware) | KD32F328, Cortex-M0 96 MHz, 128 KB flash, 24 KB RAM; FD6818 (a BK4819 clone) | receive-only at best, and only with work: below |

OpenRTX is GPL-3 and sotto is MIT, so sotto can be linked into an OpenRTX fork. The codec hook
is `openrtx/src/core/audio_codec.c`, which calls `codec2_create(CODEC2_MODE_3200)` on
160-sample frames.

### The Baofeng UV-K6 and the Quansheng UV-K5 (Cortex-M0)

*2026-11-03.* Baofeng published the K6's firmware, schematic and chip datasheets
(github.com/cnt7/BAOFENG-UV-K6-Firmware, BFPL-1.0 licence). The board: a KD32F328 MCU
(Cortex-M0 up to 96 MHz, two flash wait states there, a 4-word prefetch buffer, 128 KB flash
of which 120 KB is for the application, 24 KB SRAM, a 1 Msps 12-bit ADC, no DAC), an FD6818
RF chip with the BK4819's pins and registers, a TDA2822 speaker amplifier, external SPI NOR
flash for voice prompts.

Audio. The mic goes into the RF chip (MICP); the RF chip's demodulated audio goes to the
volume control and the speaker amplifier. **The MCU sees neither.** Its only analogue
inputs are the battery divider and a rectified VOX level. It can make sound: PA11 (TIM1
channel 4, PWM at 48 kHz carrier, updated at 8 kHz by DMA) goes through a two-pole RC filter
into the speaker path, which is how the voice prompts play. So the speaker side of a decoder
exists on a stock radio; the mic side and an RX-audio input would need wires.

Data. The RF chip's FSK packet modem runs at 1200 or 2400 baud (REG_72; the firmware ships
1200), with a preamble, sync word and CRC per packet. sotto needs 2400 bit/s of payload, so
the modem at its top rate has no room for sotto plus framing, let alone FEC.

Cycles, from a Cortex-M0 build of the fixed-point C run in the emulator (`tools/cm0_build.sh`,
`c/fixed/cm0/run.py`; the M0's published timings, the KD32F328's two flash wait states, ARMv6-M's
missing 64-bit multiply, divide and CLZ costed as library calls; bitstream and decoded samples
equal the x86 build's). MHz for real time, v3.7 at 8 kHz, ve9qrp 10 s, -O2:

| | Encoder mean / worst | Decoder mean / worst |
|---|---:|---:|
| Code in zero-wait memory | 894 / 954 | 64.8 / 82.3 |
| From flash at 96 MHz (2 wait states) | 1085 / 1138 | 78.0 / 100.1 |

| Decoder after the overlap-add rewrite below, four clips (ve9qrp, david4, vk2tpm, kristoff), from flash | | 52.5-60.9 / 73.4-88.5 |
| The same with the 0.8 KB overlap-add kernel copied to RAM | | 48.9-56.2 / 66.8-79.8 |

The decoder figures needed two changes, bit-exact on every target. The first: `FX_NARROW_MUL` (fx.h, on
for ARMv6-M) forms 16 x 16-bit products in 32 bits before widening them, because on the M0 a
product formed in 64 bits is a call to `__aeabi_lmul`. It took the decoder from 188 to 65 MHz
(zero wait). The second, also under `FX_NARROW_MUL`: the overlap-add (`fx_oa_block`) runs harmonic by harmonic
into all 160 sums, with the table index stepping by the harmonic's bin, and reads cos and sin from
one word of the packed 2 KB table (`fx_cs512`, in RAM; `-DFX_OA_UNPACKED` reads `fx_cos512`
twice instead). The kernel went from 39 to 24 MHz. `make -C c fixed` builds the path for x86 as
`sotto_enc_fx_m0` / `sotto_dec_fx_m0`, and `tests/test_vocoder.py::test_fixed_point_m0_path_is_bit_exact`
checks it against the default build, lost frames included. Next are the minimum-phase sums (16 MHz).
A frame-by-frame replay shows that real time needs only the mean under budget plus a few
milliseconds of audio buffer: a decoder held to 64 MHz never needs more than 8 ms on these clips. The encoder has many more 64-bit products, but even if every one were
free it would still need about 420 MHz: over four times the chip.

Memory. The decoder needs 840 bytes of state and 3.4 KB of stack (2.3 KB before the
overlap-add rewrite, which keeps all 160 sums), plus the 2 KB cos/sin table; the decoder alone is 57 KB
of flash at -O2 (14 KB code, 44 KB tables, 30 KB of it the codebook) or 50 KB at -Os. The
encoder's state is 34 KB, more than the K6's whole RAM.

**Verdict.** The K6 cannot be a sotto radio: the encoder needs about ten times the CPU and
more RAM than the chip has, and the mic never reaches the MCU. It can be a sotto *receiver
of messages*: the decoder now needs about 60 MHz mean of the 96, the stock FSK modem can carry
the bits slower than real time (a message, not live voice), 300 bytes of RAM hold a second of
speech, and the PWM pin plays it. No hardware change and no flash write per message. That receiver is now built:
[`docs/k6-receiver.md`](k6-receiver.md) (library `c/k6/`, Aura firmware patch `firmware/k6-aura/`). The
Quansheng UV-K5 family is the same idea at half the clock and half the flash: no.

## Suggested order

1. **OpenRTX on an MD-UV380/390.** Swap Codec 2 for the fixed-point sotto in
   `audio_codec.c`, built -O2, and time it with the DWT cycle counter to confirm the model.
   v3.8 also needs 16 kHz audio in and out, and a check that the radio's audio path passes
   4-8 kHz.
   Then make on-air tests against Codec 2 3200 over M17.
2. **The FTM-300D's DSP JTAG.** A TI XDS110 probe on J9001, a JTAG scan, then sotto loaded
   into DSP RAM with Code Composer Studio. That route needs neither the encrypted DSP update
   nor a firmware change. It is unproven: the main board could hold TRST low, and reaching the
   60-pin connector (Panasonic AXK5F60547YG) may take test pads or an interposer.
3. Skip the FT-65/FT-4X (no DSP) and the FT-70D (too slow for the encoder, no JTAG).
