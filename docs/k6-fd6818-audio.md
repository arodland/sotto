# What the K6's MCU can see of the FD6818's audio

Jeff asked on 2026-11-06 whether the mic level register moves fast enough to sample speech,
then asked for a look through the FD6818 programming guide (both the English translation and
the Chinese original, REV.1.0.0, in the project files). This page has the answer, the meter
readings from the two on-air runs, and one untested lead: an I2S port.

Some caveats about register numbers:

- Jeff's chip reads ID 0x4819 and follows the BK4819 register map (Aura's driver and the CW
  work use BK4819 addresses). Below 0x80 the guide mostly agrees with that map (REG_0B, REG_5F,
  REG_64, REG_65 and REG_67 all behaved as the guide says), but not everywhere: the guide calls
  REG_24 reserved, while on the radio it is the DTMF/SelCall set-up.
- Guide registers above 0x7F (REG_87H, REG_94H, REG_9AH, ...) are reached on the 3-wire bus
  by writing REG_7F = 1 (page 1) and then the low 7 bits. Our code reaches the same functions
  at other page 0 addresses (the tone detector at REG_24, the level at REG_6F).

Treat any guide address that hasn't been tried on the radio as a hypothesis.

## Short answer

- **Mic speech: no.** The chip gives the MCU no samples of the mic signal. Its digital
  outputs are decoder results and meters, and audio otherwise leaves the chip only as analog:
  through the AF DAC to the speaker, or into the modulator. So the K6 still can't capture
  speech to send sotto.
- **Received audio: maybe.** The guide describes an MCBSP/I2S port whose data can be the
  FM-demodulated audio (or IQ), routed to the chip's GPIO pins. On the K6 one of those pins
  (GP0) is wired to an MCU input (PB0). That could give received audio samples with no
  hardware change. It's untested.

## What the MCU can read (FD6818 guide)

| What | Register (guide) | Kind |
| --- | --- | --- |
| FSK packet data | REG_5F, 64-word RX and TX FIFOs | demodulated bits, after the chip's own FSK demodulator |
| DTMF/SelCall symbol | REG_0B[12] ready, [11:8] code | decoder result (the CW listener uses it) |
| CTCSS/DCS | compare result, code output on a GPIO | decoder result |
| VOX mic level | vox_amp REG_64[9:0] | meter (mic side) |
| Compander audio level | cmpd_db_out REG_87H[6:0], 1 dB/step | meter |
| RSSI | rssi_sq REG_67[8:0], 0.5 dB/step, dBm = v/2 - 160 | meter |
| Noise, relative RSSI | REG_65[7:0], [15:8] | meter |
| SNR | REG_61[15:8] | meter |
| LNA / AGC RSSI | REG_62 | meter |
| Flags | REG_0C: [0] interrupt, [1] squelch, [2] VOX, [7] weak signal | flags |

The guide's "bypass" mode (for DMR/dPMR) and "DISC baseband" mode route the mic ADC or the
discriminator out of the AF pin or straight to the modulator, for an external modem chip.
They are analog paths, the same as wiring AF out into an MCU ADC (a hardware change, which
Jeff ruled out).

The fastest meter is the compander level. Its detect interval is cmpd_ct_intvl (guide
REG_9AH[13:8], 0.64 ms per step), so at most about 1,500 readings a second, each a loudness
in dB. That is an envelope with no sign or pitch, useless for speech even at that rate.

## Meter readings on the air (hamlet, 440.850 MHz, 2026-11-06)

From the cwprobe run, 05:09-05:19 UTC, image 6473ca7. Registers were polled every 2 ms for
600 s while the IC-7100 sent a module upload (FFSK) and CW IDs at 20 wpm/800 Hz,
13 wpm/600 Hz and 25 wpm/1200 Hz. Full-run ranges (`R` lines):

| Register | Min | Max | Reading |
| --- | --- | --- | --- |
| REG_6F | 0x0023 | 0x1a5b | [5:0] 35 dB with carrier vs 27 dB, levels 26-33 dB in the ID; bits 6 and up vary |
| REG_64 | 0x000a | 0x000a | never moved: it is vox_amp, the mic level |
| REG_65 | 0x0007 | 0x0050 | noise_sq [7:0] from 7 to 80 (dB steps); rssi_rel [15:8] stayed 0 |
| REG_67 | 0x008a | 0x0136 | RSSI -91 to -5 dBm (no carrier vs the IC-7100 next door at RFPOWER 0) |
| REG_0B | 0xc006 | 0xdfa6 | tone detector: 0xC006 idle, 0xD006 with tone 0, 0xDFA6 with tone 15 |
| REG_0C | 0x0200 | 0x0203 | bit 0 interrupt, bit 1 squelch |

- **REG_6F, the audio level:** it moves only about every 120 ms. The 20 wpm ID's marks came
  out at 104-128 and 340-356 ms, and its spaces at 132, 240, 360, 472, 600, 724 and 852 ms, all
  multiples of about 120 ms, against a 60 ms dot. Key up and key down were 7 dB apart, with a
  noise estimate of 0 dB. The decoder read "3 wpm", text `#`. The compander's detect interval
  above might be what sets this; not tried.
- **REG_64:** flat for the whole run, through carrier, FFSK and CW: it measures the mic, not
  the receiver.
- **The tone detector (REG_0B):** edges within about 10 ms. The 13 wpm ID's first marks and
  spaces, sent 92/92/277/277/277/92/277 ms, read 102/82/288/266/286/83/286 ms. Each tone-on
  also raised the interrupt with cause 0x0800 in REG_02.

From the cwlisten runs, 05:30-05:35 UTC (REG_0B bit 12 only, decoded):

| ID | Lag | Dot / dash heard, ms (sent) | Decodes |
| --- | --- | --- | --- |
| 20 wpm, 800 Hz | 12 ms | 68-72 / 188-192 (60 / 180) | 2 of 2 exact, tone 800, q=100 |
| 13 wpm, 600 Hz | 12 ms | 100-104 / 284-288 (92 / 277) | 2 of 2 exact, tone 600, q=100 |
| 25 wpm, 1200 Hz | 8 ms | 52-56 / 148-156 (48 / 144) | 2 of 2 exact, tone 1200, q=100 |

The module upload's FFSK triggered tone 15 (1350 Hz) in 1-3 mark bursts of 16-52 ms, 3 to 7
per run. See [k6-cwid.md](k6-cwid.md) for the decoder and its host-test statistics.

## The I2S lead

From the guide's register summary:

- mcbsp_en, REG_04H[0]: "1=Enable MCBSP/I2S module".
- REG_75H (default 0x0FB4) and REG_76H (default 0xE307) configure it:
  - master or slave, and frame width
  - clock polarities and a clock divider (mcbsp_clkgdv, REG_76H[7:0])
  - a FIFO bypass for master mode
  - mcbsp_dsel, REG_76H[11]: "MCBSP/I2S data output select. 1=IQ; 0=FM Demodulated"
- The GPIO output selects can each route one signal: gpio0_out_sel is REG_26H[3:0], and
  gpio7-4 are REG_27H. The values are 12 = I2S/MCBSP DOUT, 13 = DFS (frame), 14 = DCLK. Other
  values are gpio_out_val, INT, SQ, VOX, the CTCSS/DCS compare, 5 = the CTCSS/DCS code output
  "for software decode", and 6 = SDO for 4-wire SPI. A separate register selects GPIO inputs
  for slave-mode clock, frame and data in.

The K6 schematic (BF_K6_Plus_Open_V1.1, `ht-survey/uv-k6-rf-chip-audio.png` and
`uv-k6-mcu-beep-pwm.png` in the project files) wires the FD6818's GPIOs as follows:

| Pin | Goes to |
| --- | --- |
| GP0 | R175 (100 ohm), net CTCSS, MCU pin 19 (PB0) |
| GP2 | U3T_EN |
| GP3 | V3T_EN |
| GP5 | R_LED |
| GP4, GP6 | lines off the crop (band or PA switching) |
| GP1, GP7 | not connected |

The guide's default for REG_26H is 0x0005, so out of reset GP0 carries the CTCSS/DCS code for
the MCU to decode in software, which is what the net name says. PB0 is the only FD6818 GPIO
that reaches the MCU.

So received audio could, in principle, reach the MCU like this:

1. The I2S port in master mode, data = FM demodulated.
2. DOUT on GP0, read on PB0.

There is only one wire, though. The MCU would get the data line without the frame or bit
clock, and would have to recover the timing itself: sample PB0 several times faster than the
bit clock (an 8 kHz, 32-bit frame is 256 kbit/s), find the frames, and track the drift between
the radio's 26 MHz and the MCU's 16 MHz crystals.

What it would be good for: received audio samples would let the K6 run its own software
modem (faster than the chip's FSK, which slips at 2400 baud) and receive analog audio
digitally. It still gives no mic samples.

**First test.** Does a 0x4819 chip have the port at all?

- The BK4819 map may use REG_04, REG_26, REG_75 and REG_76 for other things.
- A level 2 module could read their reset values, enable the port, put DFS (13) on GP0, and
  count PB0 edges by reading the MCU's GPIO input register directly. About 8,000 per second
  would mean the port is real.
- API level 2's fd_write refuses only 0x30, 0x33, 0x36 and 0x37, so these writes are allowed.
- The firmware re-arms its set-up when the module returns, but REG_26 should be put back to
  0x0005 explicitly.
