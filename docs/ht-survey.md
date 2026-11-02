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
| Quansheng UV-K5 family (own open firmwares) | Cortex-M0 48 MHz, 16 KB RAM | no |

OpenRTX is GPL-3 and sotto is MIT, so sotto can be linked into an OpenRTX fork. The codec hook
is `openrtx/src/core/audio_codec.c`, which calls `codec2_create(CODEC2_MODE_3200)` on
160-sample frames.

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
