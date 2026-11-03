# Sending sotto voice messages to a Baofeng UV-K6 from an IC-7100

*Written 2026-11-03. Code: `modem/pager.py`, `modem/fsk.py`, `modem/cal.py`, `modem/civ.py`,
command line `python -m modem.k6`. Tests: `tests/test_k6.py`. Nothing here has been on the
air yet. The first session with the radios is calibration, not voice.*

The K6 "voice pager" receives a sotto message as data through its RF chip's FSK packet modem,
keeps it in RAM and plays it (design: the K6 voice-pager memo; the receiver firmware is in
[`docs/k6-receiver.md`](k6-receiver.md)). This softmodem is the transmit side. It packs a `.sot` file into the
chip's packets and makes audio for the IC-7100, which transmits it on FM.

## The packet format

The softmodem implements the **K6 voice-pager FSK packet format, version 1**, which is shared
with the receiver firmware. The test vectors in that spec are in `tests/test_k6.py`.

- **On air:** 0x55 x 16, sync `07 6B 3E 36`, then a 64-byte payload, MSB first. The chip's own
  CRC and scrambler are off, so all checking is ours.
- **Payload (profile L):** magic 0xA7, msg_id, packet number, total frames, 8 sotto frames
  (48 bytes), 8 reserved bytes, CRC-16/CCITT-FALSE. PN9 whitening over all 64 bytes.
- **Interleave:** 8 packets per group. A lost packet costs 8 isolated 20 ms frames 160 ms
  apart, which sotto's concealment bridges.
- **Profile S** is the 16-byte fallback (`--payload S`), only if the FD6818 won't take 64-byte
  frames.
- **Timing:** 400 ms of 0x55 after keying, carrier and 0x55 kept up between packets, 2 bytes
  of 0x55 before unkeying.

| | Air time per packet | Speech per packet | 15 s message |
|---|---:|---:|---:|
| 1200 baud, profile L | 560 ms | 160 ms | 54 s |
| 2400 baud, profile L | 280 ms | 160 ms | 27 s |
| 1200 baud, profile S | 240 ms | 40 ms | 90 s |

## Two ways to put the bits on air

| | `--phy D` (primary): direct NRZ 2-FSK | `--phy F` (fallback): FFSK 1200/1800 Hz tones |
|---|---|---|
| What the chip expects | The carrier shifted by ±deviation (REG_58 = 0x00E1) | MDC1200-style tones (REG_58 = 0x3FE3) |
| IC-7100 input | **DATA2** (rear 6-pin mini-DIN) with **9600bps Mode** on, from a sound card | Anything: the built-in **USB audio** in FM-D, or DATA2 at 1200 |
| Why | The audio *is* the frequency shift, so the input must be flat down to about 20 Hz. A voice input's 300 Hz high-pass smears runs of equal bits | Tones live inside the voice band, so pre-emphasis and the high-pass don't matter |
| 2400 baud | Yes (`--baud 2400`), if the chip copes | No |

Simulated through models of the IC-7100's paths (`tests/test_k6.py`; our demodulator stands in
for the chip):

- Profile D through a 9600-style input (first-order 20 Hz coupling) decodes every packet.
- Profile D through a voice path (pre-emphasis plus a 300 Hz high-pass) loses most packets.
- Profile D pre-compensated from a measured response (`--comp`, below) works again through a
  gentle (second-order) high-pass. A steep (fourth-order) one is still mostly lost at 1200
  baud, but 2400 baud does better.
- Profile F decodes every packet through every path we tried.

So: **use DATA2 and profile D if you can, and profile F over USB if you can't.**

Over a simulated FM link (`tools/k6_link_sim.py`: 2.5 kHz deviation, 12.5 kHz channel, white
noise, no fading), the share of packets that decode is:

| C/N0 (dB-Hz) | D 1200 | D 2400 | F 1200 |
|---:|---:|---:|---:|
| 46 | 12% | 0% | 0% |
| 49 | 92% | 72% | 60% |
| 52 | 100% | 100% | 100% |

That is the usual FM threshold. Above it, everything decodes; below it, nothing does. Profile
D at 1200 baud has about 1-2 dB in hand over the others.

## Quick start

```sh
make                                                     # builds c/sotto_enc and c/sotto_dec
sox speech.wav -r 8000 -c 1 -b 16 -e signed -t raw speech.raw   # or ffmpeg -ar 8000 -ac 1 -f s16le
c/sotto_enc speech.raw msg.sot                           # sotto v3.7, 300 bytes per second

python -m modem.k6 tx msg.sot tx.wav --cw-id N0CALL      # profile D audio, 48 kHz
python -m modem.k6 tx msg.sot tx.wav --phy F             # FFSK, for USB audio
python -m modem.k6 rx tx.wav back.sot --good good.u8     # loopback check (or a recording)
c/sotto_dec back.sot out.raw good.u8                     # lost frames concealed

python -m modem.k6 send msg.sot --port /dev/ttyUSB0 --player "aplay -D plughw:CARD=CODEC"
```

`send` keys the radio over CI-V, plays the audio, and always unkeys, even on Ctrl-C or an
error. It plays through the `sounddevice` package (`--device`), or a command (`--player`).
`--ptt rts` or `--ptt dtr` keys with a serial control line instead: use that for the IC-7100's
USB SEND setting, or a Digirig on DATA2. `--dry-run` shows what would happen. The K6 receiver firmware holds 640 frames (12.8 s) by default, so `tx` and `send` warn about longer messages. `--max-tx`
(180 s) refuses anything longer.

Other options: `--baud 2400`, `--payload S`, `--passes 2` (the whole message twice; the K6 fills
holes from the second pass), `--msg-id`, `--invert`, `--level` (peak audio, default 0.5), and
`--lead-ms`.

`rx` reads WAV or FLAC at any rate, or SDR IQ with `--iq RATE` (complex64). It prints how many
packets were found and how many were good. It also flags packets that arrived inverted,
and gives each packet's amplitude with `-v`.

## IC-7100 settings

These are from the IC-7100 manual and users' reports, not yet checked on Jeff's radio.

**Mode.** FM-D (FM data mode), which mutes the microphone. Use a narrow FM channel to match a
K6 set to narrow, with about 2.5 kHz deviation (the spec's nominal value). Check in the
manual which FM filter setting gives narrow transmit deviation on the IC-7100.

**Profile D on DATA2** (rear 6-pin mini-DIN, from a USB sound card or a Digirig):

- Set mode > Connectors > **9600bps Mode: ON** (default OFF). This applies to DATA2, not DATA1.
- Connectors > **DATA MOD: ACC**, and DATA MOD Level 50% to start.
- DATA2 pins: 1 = DATA IN (0.2-0.5 Vp-p at 9600), 2 = GND, 3 = PTT (ground to transmit),
  4 = DATA OUT 9600 (the flat discriminator output, useful for recording), 5 = DATA OUT 1200,
  6 = squelch.
- Start with `--level 0.5` and a sound card output of a few hundred mV peak to peak. Then set
  the deviation as in the calibration below.

**Profile F over the built-in USB audio** (one USB cable for audio and CI-V):

- Connectors > **DATA MOD: USB**, and USB MOD Level about 30-50%.
- The radio's USB sound card appears as "USB Audio CODEC" with no driver needed.
- Whether 9600bps Mode also flattens the USB input is unknown. The calibration fit will show
  whether it does. If the result is flat to about 20 Hz, profile D works over USB too.

**PTT.**

- **CI-V** (default): the IC-7100's default address is 0x88. Transmit is
  `FE FE 88 E0 1C 00 01 FD` and receive is `FE FE 88 E0 1C 00 00 FD`.
- **USB SEND:** set it to RTS or DTR on the radio, then use `--ptt rts` or `--ptt dtr`.
- **DATA2 pin 3:** a Digirig drives it from RTS.
- `--set-mode` sends FM (`06 05 01`) and data mode on (`1A 06 01 01`). Check the data-mode
  command against the IC-7100 CI-V reference before relying on it; otherwise set FM-D by hand.

**Power and duty.** A 15 s message is about a minute of continuous carrier at 1200 baud. Use
low power (the K6 is in the same room for the first tests).

**Identification.** The packet format is open and documented, and the whitening is a published
PN9 sequence, not encryption. `--cw-id CALL` appends a 20 wpm Morse ID after the data, which
covers station identification (97.119).

## Calibration, step by step

This follows section 8 of the packet-format spec.

1. **Check the K6's RF chip.** CHIPID must be 0x4819, not 0x4829.
2. **K6 transmits, IC-7100 receives.** Pressing PTT on the K6 (receiver firmware) sends test packet 0 twice in D, then twice in F, on the VFO TX frequency (about 3 s). Decode the same recording once with `--phy D` and once with `--phy F`.
   Record the IC-7100's DATA2 pin 4 (9600 out, with 9600bps Mode on) on a sound card, or an
   SDR's IQ. Then run `python -m modem.k6 rx rec.wav -v` (add `--phy F` for the FFSK test).
   The packet must decode, and the output tells you:
   - whether the polarity is inverted (if so, add `--invert` on transmit),
   - whether the bit and byte order are right (a bad CRC means one of them isn't),
   - the K6's amplitude.
3. **Match the deviation.** Record the IC-7100 sending `python -m modem.k6 test test.wav` on
   the *same* flat receiver: an FTM-300D's DATA jack at 9600, or an SDR. Adjust `--level` or
   DATA MOD Level until the amplitude `rx -v` prints matches the K6's from step 2. With an SDR
   (`--iq`), the amplitude is in Hz of deviation directly.
4. **IC-7100 transmits, K6 receives.** Send `test test.wav` (test packet 0, repeated 20 times).
   The K6 shows SyncP or SyncN, the CRC result and the magic byte.
5. **Then a whole message.** First the 70-frame test message (`test --message`), then a real
   clip, then packet loss against distance, then `--baud 2400`.

**Measuring the modulation path** (needed only for profile D through an input that isn't
flat):

```sh
python -m modem.k6 cal cal.wav                  # 1 kHz reference, then 6 s of multitone
#   transmit cal.wav exactly as the modem will (same input, same levels) and record a flat
#   receiver: FTM-300D DATA jack (9600) or SDR IQ
python -m modem.k6 fit rec.wav ic7100.json      # or: fit rec.cf32 ic7100.json --iq 240000
python -m modem.k6 tx msg.sot tx.wav --comp ic7100.json
```

`fit` prints the response relative to 1 kHz, from 20 Hz to 5 kHz, and the polarity. With IQ
input, it also prints the deviation the reference tone made and the `--level` for 2.5 kHz. The
receiver's own response is part of the measurement, so use the flattest receiver available.
The compensation's gain is capped (`--max-gain`, 30 dB above 1 kHz), because a high-pass
can't be undone all the way down to DC.

## What's assumed, and how it gets settled

| Assumption | Settled by |
|---|---|
| Bit 1 = +deviation (D) or 1200 Hz (F) | Calibration step 2 (`rx` reports inverted packets) |
| FIFO words go high byte first | Step 2 (the magic byte after de-whitening) |
| The chip accepts 64-byte frames | Step 4; fall back to `--payload S` |
| ±2.5 kHz deviation suits the K6's receiver | Steps 2-3 (match the K6's own transmitter) |
| The IC-7100's DATA2 9600 input is flat to ~20 Hz | `cal` / `fit` |
| Our demodulator is a fair stand-in for the chip | The first on-air tests |
