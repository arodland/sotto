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
- Known mappings from Aura's working driver: the guide's REG_03H (block enables) is REG_30
  here, REG_25H (GPIO output enable and value) is REG_33, REG_28H (PA) is REG_36, REG_94H (tone
  detector) is REG_24, and REG_87H (AF level) is REG_6F. Known clashes: REG_26, which the guide
  calls the GPIO3..0 output select, gets Aura's 0x13A0 distortion setting here; REG_75, the
  guide's MCBSP set-up, is an RX audio filter coefficient here. So the I2S registers below are
  probably at other addresses on this chip (the I2S probe thread is testing which).
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
  (GP0) is wired to an MCU input (PB1). That could give received audio samples with no
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
| GP0 | R175 (100 ohm), net CTCSS, MCU pin 19 (PB1; pin 18 is PB0, keypad row L0) |
| GP2 | U3T_EN |
| GP3 | V3T_EN |
| GP5 | R_LED |
| GP4, GP6 | lines off the crop (band or PA switching) |
| GP1, GP7 | not connected |

The guide's default for REG_26H is 0x0005, so on a 0x6818 GP0 would carry the CTCSS/DCS code
for the MCU to decode in software, which is what the net name says. On Jeff's radio GP0's
output is off (REG_33 = 0xd300, see the probe below). PB1 is the only FD6818 GPIO that reaches
the MCU. (An earlier version of this page said PB0: wrong, PB0 is pin 18.)

So received audio could, in principle, reach the MCU like this:

1. The I2S port in master mode, data = FM demodulated.
2. DOUT on GP0, read on PB1.

There is only one wire, though. The MCU would get the data line without the frame or bit
clock, and would have to recover the timing itself: sample PB1 several times faster than the
bit clock (an 8 kHz, 32-bit frame is 256 kbit/s), find the frames, and track the drift between
the radio's 26 MHz and the MCU's 16 MHz crystals.

What it would be good for: received audio samples would let the K6 run its own software
modem (faster than the chip's FSK, which slips at 2400 baud) and receive analog audio
digitally. It still gives no mic samples.

**Against it:**

- The BK4819 datasheets have no MCBSP block.
- In the BK4819 register headers that UV-K5 firmware uses, 0x75 is an RX audio EQ, not the
  guide's MCBSP set-up.
- Jeff's chip follows that map wherever we've checked.

SPI speed would not be the limit. The chip allows an 8 MHz clock, and Aura's bit-banged driver
at 2 us a bit manages roughly 18,000 register reads a second (an estimate). That would be
plenty if a register held samples, but none does.

**First test.** Does a 0x4819 chip have the port at all?

- The BK4819 map may use REG_04, REG_26, REG_75 and REG_76 for other things.
- A level 2 module could read their reset values, enable the port, put DFS (13) on GP0, and
  count PB1 edges by reading the MCU's GPIO input register directly. About 8,000 per second
  would mean the port is real.
- GP0's output has to be switched on first, which is a REG_33 write; level 2 refused REG_33
  until the image change below.

## The I2S probe (`c/k6/mod/i2sprobe.c`)

The module reads PB1 straight from the MCU's GPIOB input register (0x48000410; it writes no MCU
register) in a tight loop, about 6.3 million samples a second, and counts edges in 100 ms
windows. It dumps registers 0x00-0x7F (not 0x5F, whose read pops the FSK FIFO), then:

1. Switches GP0's output on (REG_33 bit 8 clear).
2. Tries all 16 values of REG_34[3:0]. On the 0x4819 the guide's GPIO block seems to sit
   0x0E higher (guide 25H = 0x33, 28H = 0x36, as Aura uses them), so guide 26H, the GPIO3..0
   output select, would be 0x34. Guide code 15 (XTAL/2, 13 MHz) would flood PB1 with edges.
3. With GP0 on DFS, DCLK and DOUT (13, 14, 12), tries no enable, REG_31 bit 0 and REG_04
   bit 0 as mcbsp_en, printing the first 32 edge gaps of each window (the raw capture).

It puts every register back and refuses to write unless PB1 is an input.

**Run 1, 2026-11-06 06:02 UTC (image 6473ca7):** ret -5, nothing written. REG_33 = 0xd300:
enable-bar set for GP0, GP1, GP4, GP6 and GP7, clear for GP2, GP3 (band) and GP5 (LED), which is
Aura's own set-up. With GP0 not driving, PB1's pull-up (PUPDR 01 on the radio) read high in all
630,784 samples. Other readings: REG_34 = REG_35 = 0x0000; REG_26 = 0x13a0 (Aura's value, so it
is not the GPIO select here, as it would break band switching); REG_75 = 0xd42d and REG_76 =
0xf50b, not the guide's 0x0FB4/0xE307 (on the BK4819 map 0x75 is an RX audio filter), so the
guide's I2S set-up isn't at those addresses; REG_31 = REG_04 = 0x0000.

**Image change (PR from this thread):** a module may change REG_33 bits 8 and 0 (GP0's enable
and value) and nothing else in it; the firmware puts REG_33 back when the module returns.
GP0 only reaches PB1, an MCU input.

**Run 2, 06:13 UTC (image e46e7bd, `i2sprobe`):** REG_33 d300 -> d200 put GP0's output on, and
the sweep showed REG_34[3:0] is GP0's output select (the guide's 26H, moved by 0x0E). With no
carrier, per 100 ms window at about 6.3 M samples/s:

| Code | PB1 | Code | PB1 |
| --- | --- | --- | --- |
| 0 | low (out_val bit 0) | 8 | low |
| 1 | high | 9 | 20 edges, slow |
| 2-7 | low | 10 | high |
| 11 | 93 edges, slow | 12, 13, 14 | about 340,000 edges, 50% high |
| 15 | 188,000 edges | | |

No window gave the 1,600 edges an 8 kHz frame clock would. REG_31 bit 0 changed nothing and
REG_04 doesn't hold a write.

**Run 3, 06:21 UTC (`i2scap`, 22.9 M samples/s bursts of 12,000 samples):** codes 12, 13 and 14
give one and the same signal, also with REG_31 bit 0 set: high and low runs both cluster at 3-4,
7-8 and 11-12 samples (units of about 160 ns, 26 MHz / 4), never longer, 50% ones. That is a
bitstream, not a clock or a frame pulse. Code 15 is mostly 1-sample runs (13 MHz, aliased).
Codes 9 and 11 change only on a 25.39 kHz grid (26 MHz / 1024): every gap between edges is a
whole multiple of 286 samples (39.4 us) of the slow counter.

**Run 4, 06:34 UTC (`gp0tone`, the IC-7100 sending a carrier, then 1000, 1800 and 600 Hz):**
the density of code 13 (ones per 256 samples, 11.2 us) is the receiver's low IF:

| Signal | Code 13 density | Codes 11 and 9 |
| --- | --- | --- |
| no carrier | flat at 50%, a few % of noise | about 900 and 200 edges/s |
| bare carrier | a clean sine, 65-195 of 256, at 16.9-17.0 kHz | silent |
| 600/1000/1800 Hz tones | the same sine at 13.3-20.7 kHz, about 17 kHz plus the deviation | silent |

So the chip has **no I2S port** here, but GP0 code 13 hands the MCU a 1-bit sigma-delta stream
of the receive IF (17 kHz is 26 MHz / 1536). Codes 9 and 11 behave like noise or squelch
detector outputs. Software FM demodulation of that IF would give the MCU received audio; the
limit is the CPU: the stream is about 6.5 Mbit/s on one pin with no clock, so the MCU must
sample it by polling. `gp0fm` decimates while it samples (one byte per 256 samples, about
89 k a second) so the module store holds about 28 ms of IF for the host to demodulate.

**Run 5, 06:41 UTC (`gp0fm`, CI build afc6744, the IC-7100 sending 0.3 s of carrier then
1000 Hz):** two captures of 2512 bytes, each 27.8 ms at 90.23 k bytes/s (11.08 us a byte, from
the calibration: 10 captures in 278.4 ms). On the host: subtract the mean, band-pass 8-26 kHz,
Hilbert transform, instantaneous frequency, low-pass at 3 kHz.

| Capture | When | IF | Demodulated |
| --- | --- | --- | --- |
| 0 | 0.2 s after PTT, bare carrier | 16.919 kHz | flat, 31 Hz rms, no tone |
| 1 | 2.4 s after PTT, in the tone | 16.907 kHz | a 1000.9 Hz sine, +-4.3 kHz deviation, 171 Hz rms residual (about 25 dB SINAD in 0.3-3 kHz) |

So **GP0 code 13 is a working FM receive path into the MCU**: the decimated density is a
~16.9 kHz IF that demodulates in software to the transmitted audio, with no hardware change.
The capture is a busy loop (the CPU does nothing else while it samples), so the limits are CPU
time and RAM, not the signal. The bytes and plot are on hamlet (`~/k6-logs/gp0fm-20261106T0641*`).

## Capturing the IF by DMA (`gp0raw`, `gp0dma`, `gp0sweep`, PR #69)

**No hardware path on PB1.** The KD32F328 follows the STM32F030 alternate-function map (Aura's
SPI1, SPI2, USART1 and TIM1 pins all match it), so PB1 offers only TIM14_CH1, TIM3_CH4,
TIM1_CH3N and ADC_IN9: no SPI or USART input to shift the stream in, and no timer that can be
gated or clocked by it. What is left is DMA reading GPIOB IDR into RAM. Jeff OK'd modules
using TIM17 and DMA channel 1 (both unused by Aura and the pager; the pager's PWM audio is TIM1
and channel 5).

**Run 6, 07:37 UTC (`gp0raw`, 0.88 ms of the raw 22.9 MS/s stream):** `tools/k6_gp0.py rate`
keeps every k-th sample to see how slowly the stream can be sampled. The stream's runs are
whole multiples of about 156 ns, so GP0 is clocked at about 26 MHz / 4 = 6.5 MHz.

| Sample rate | FM noise, bare carrier | Residual in a 1 kHz tone (sine fit) |
| --- | --- | --- |
| 22.9 MS/s | 60 Hz rms | 285 Hz rms |
| 11.4 MS/s | 89 Hz | 367 Hz |
| 5.7 MS/s | 166 Hz | 427 Hz |
| 4.6 MS/s | 413 Hz | 792 Hz |
| 2.9 MS/s | 625 Hz | 1266 Hz |

**Run 7, 07:45 UTC (`gp0dma` v1):** DMA channel 1 with TIM17's update as its request, TIM17 ARR
set for 24 to 1.5 MS/s. Every setting ran at the same 5.71 M transfers/s: TIM17 doesn't pace
the channel. The CPU decimated and demodulated the ring as it filled, and at L = 64 samples an
output (the setting nearest the real rate) the K6 itself read the 1 kHz tone at ±4.0 kHz on a
16.93 kHz IF, with nothing lost.

**Run 8, 08:09 UTC (`gp0dma` v2):** the module lets the DMA run flat out and times itself
with TIM17's counter (0..0xFFFF at 96 MHz). Its probe counted transfers in 2 ms:

| TIM17 | off | UDE only | UDE, counting | same, clearing SR | UDE, 1 MHz update | CC1DE, 1 MHz |
| --- | --- | --- | --- | --- | --- | --- |
| transfers | 10944 | 11268 | 11269 | 11608 | 11269 | 11268 |

So channel 1 runs at 5.5-5.8 M transfers/s even with TIM17 off: on this chip an enabled channel
1 is always requested (by what is not known; the ADC is clocked but has no DMAEN). There is no
pacing to be had, and the free-running rate happens to sit at the usable floor of run 6.
(An earlier attempt refused with -7 because the DMA's registers read 0x37 while its clock is
off; the module now clocks the DMA before it looks.)

| L (samples an output) | Output rate | 3 s / 1 s continuous, 1 kHz tone at peak 0.3 | CPU idle | Overruns |
| --- | --- | --- | --- | --- |
| 64 | 85.7 kHz | IF 16.934-16.960 kHz, tone 996-1006 Hz, ±4.03-4.18 kHz, all 30 blocks | 72-73% | 0 |
| 32 | 174 kHz | ±17-20 kHz: noise (33 density levels) | 56% | 0 |
| 128 | 42.5 kHz | IF biased low, tone 6-12% high | 93% | 4 |

**L = 64 is the working set-up**: about 5.49 MS/s, one output per 11.7 us, the receive IF
demodulated continuously on the MCU with about 27% of the CPU.

**Run 9, 08:10 UTC (`gp0sweep`, `k6_gp0.py wav`: 24 tones 150-5000 Hz, 0.6 s each, at WAV peak
0.1 then 0.3):** the received deviation, which mostly measures the IC-7100's TX audio chain in
FM-D (DATA on, USB audio in), relative to the 935 Hz step:

| Tone (Hz) | 150 | 237 | 321 | 508 | 935 | 1477 | 2003 | 2717 | 3165 | 3686+ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| peak 0.1 | -17 dB | -11.8 | -8.9 | -5.0 | 0 (±1.8 kHz) | +3.4 | +5.1 | +5.8 | -3.3 | none |
| peak 0.3 | -13.7 | -9.5 | -6.9 | -3.1 | 0 (±4.3 kHz) | -0.5 | -1.0 | -1.8 | -2.5 | none |

- Below the limiter (peak 0.1) the deviation rises about 5 dB an octave from 300 Hz to 2.3 kHz:
  pre-emphasis on the USB input, or something like it.
- Nothing above about 3.2 kHz reaches the air: the TX audio low-pass is a wall there.
- At peak 0.3 the deviation flattens at about ±4.3 kHz from 700 Hz up: the IC-7100's limiter.
  The 1 kHz tone at peak 0.3 used in runs 5-8 is already about 2 dB into it.
- REG_6F[5:0] is no level meter: 37-38 on a bare carrier, 10-23 with tones, falling with
  frequency, and a step between 276 and 321 Hz that fits the chip's ~300 Hz audio high-pass.
- The module's tone counter holds from about 240 Hz (150 Hz at the higher level) to 2.7 kHz.
  Its `dev` swing reads low above about 1.7 kHz (the 4-cycle average); use `rms`.

For the FFSK pager this means: a tone above 3.2 kHz never leaves the IC-7100, and between 300
Hz and 2.3 kHz the higher FFSK tone goes out about 5 dB an octave hotter than the lower one
unless the sender de-emphasises it. Logs and tables are on hamlet (`~/k6-logs/gp0raw-*`,
`gp0dma*-*`, `gp0sweep-20261106T0809*`).
