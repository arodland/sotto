# K6: transmitting FSK the MCU writes itself

The K6 already transmits our packets with the FD6818's own FSK modem: the calibration bursts
of 2026-11-04 decoded bit-perfect on the IC-7100 in D (NRZ 2-FSK) and F (FFSK 1200/1800). That
modem only sends two levels, at the rates its REG_58/REG_72 allow, from a 64-word FIFO. This
page is about the MCU choosing the transmit frequency itself, symbol by symbol, so the K6 can
send 4FSK, shaped (GFSK-like) symbols, any rate the synthesizer can follow, and long frames.

## What we checked (2026-11-06)

Jeff passed on notes from another source. The guide (FD6818 programming guide, project
files), Aura (`src/drivers/fd6818.rs` at edf3b98) and egzumer's UV-K5 firmware say:

| Claim | Verdict |
| --- | --- |
| TX is constant-envelope FM; a plain carrier is TX with nothing modulating | True |
| CW = key the PA with the PLL locked | True; Aura's CW mode does it (REG_36 PA on/off) |
| Deviation lives in "REG_36, REG_37, REG_3D" | No: REG_36 = PA, REG_37 = power/LDO enables, REG_3D = IF. Deviation is REG_40: [12] dev_en, [11:8] dev_sh, [7:0] dev_lvl, gain (256 + dev_lvl) >> dev_sh |
| A "direct FM source" register: write a value, get that frequency offset | Not in the guide, Aura or egzumer |
| A TX_TEST / CW bit | Not found |
| XTAL calibration in REG_07 | No: REG_07 is the CTCSS/DCS frequency. The crystal is REG_3B/3C |
| PA ramp via REG_3A | No: REG_3A is the 25/12.5 kHz setting (0x00E0 / 0x0040); PA gain is REG_36 |
| The UV-K5's M17 firmware sends 4FSK this way | Found no such firmware |
| "Check the FD6818 address, don't trust the BK4819 one" | Backwards for this chip: 0x4819 follows the BK4819 map (see the project's register map notes) |

## The route: pull the crystal

The guide's DMR set-up says "if the transmission is realized by adjusting the external
TCXO, also set REG_40 = 0 (internal modulation off)": DMR radios on this chip family make
4FSK by pulling the reference oscillator. The K6 has a plain 26 MHz crystal, but the chip
keeps its idea of the crystal frequency in a register:

    xtal = ((REG_3C >> 8) << 16 + REG_3B) * 5 Hz        26 MHz = 0x4F5880: REG_3C 0x4F00, REG_3B 0x5880

egzumer's F CAL menu writes REG_3B = 22656 + offset (offset -1000..+1000) live. The
synthesizer works out its divider from the wanted frequency and this crystal value, so a step
of 1 in REG_3B moves the RF by f x 5 Hz / 26 MHz:

| Band | Hz per REG_3B step | 4FSK +-600 / +-1800 Hz |
| --- | --- | --- |
| 440.850 MHz | 84.8 | +-7 / +-21 steps (594 / 1781 Hz) |
| 146 MHz | 28.1 | +-21 / +-64 steps |

The open question is whether a REG_3B write takes effect at once or only at the next
RXON/TXON (the guide says a new REG_38/39 frequency needs RXON or TXON). Aura never writes
REG_3B (it folds its crystal correction into the frequency word), so nothing on the K6 tells us
yet.

## xtalstep: the receive-only test

`c/k6/mod/xtalstep.c` answers it without transmitting. The receiver's LO comes from the same
synthesizer, and since PR #68/#69 the MCU can see the received IF on PB1 (GP0 code 13, DMA
channel 1). With the IC-7100 sending a bare carrier, the module:

- **A**: sets REG_3B to base + k for k = 0, 6, -6, 0, 24, -24, 0, 48, -48, 0 and measures the
  mean IF each time (about 92 ms). Live means about 85 Hz per step at 440.850.
- **B**: moves REG_38 by +-100 (1 kHz) without re-RXON, to confirm the guide.
- **L** (only if A saw nothing): REG_3B + 24, then a retune to the same frequency (RXON).
- **C** (only if A moved): squares REG_3B between base + 24 and base - 24 every 2.15 ms, 1000
  times, and averages each IF cycle's frequency by its time after the write began, in 45 us
  bins over 0.7 ms. That is the synthesizer's step response, which sets the symbol rate.

AFC is off for the run (REG_73 bit 4); everything is put back afterwards. Tests:
`tests/test_k6_xtal.py` runs it on the emulated M0 against a chip model with a live register
(60 us lag) and a latched one.

### Bench run

1. K6 in VOICE MSG on 440.850, serial cable in, the listener logging ttyUSB2 (read only).
2. Send the module (CI artifact `mod-xtalstep.bin`, about 2.8 KB):
   `python -m modem.k6 mod xs.wav mod-xtalstep.bin --api-level 2 --key-file ~/.config/sotto/k6-mod.key`
   and play `xs.wav` as for any module.
3. When the K6 says `xs wait carrier`, key the IC-7100 on 440.850 with no audio for about
   15 s (the run takes about 1.5 s for A and B, and 4.3 s for C).
4. The log's `xs a`, `xs b`, `xs l` or `c up/dn` lines are the result.

## Next

If REG_3B is live: a module transmit API (level 3), then a TX module that sends the existing
packet format (PHY D framing: preamble, sync, PN9 payload, CRC) by writing REG_3B per symbol,
received by `modem.k6 rx` on the IC-7100, and then 4FSK. If it is latched: the fallbacks are the
tone oscillator (REG_70/71, audio FSK only) and the chip's own FSK modem, which already works.
