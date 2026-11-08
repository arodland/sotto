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
  apart, which sotto's concealment bridges. `--depth 4` (or 2, 1) sends smaller groups: the
  top 2 bits of the packet number say which (0 = 8, 1 = 4, 2 = 2, 3 = 1), so the receiver
  adapts per message. A streaming K6 starts once a group is in, so depth 4 starts about twice
  as soon; a lost packet's frames are then 4 slots apart. Images before 2026-11-05 know only 8.
- **Short last group** (2026-11-07, default; `--no-short-last` for the old layout): a last group
  with only rem frames takes m = ceil(rem / F) packets instead of D, packet k carrying frames
  g*F*D + s*m + k, and sets bit 13 of the packet number (seq is now 13 bits; the most a message
  needs is 8192 packets). The receiver gets m from total. Images before 2026-11-07 drop those
  packets (they read seq >= 8192) and still play every full group. On 25 real clips: XL 9.2%
  fewer packets in sotto 2400, 15.2% in sotto 1200; L 4.8% / 7.1%.
- **Trim** (`tx`/`send`, default; `--no-trim`): dead air goes from both ends of the message,
  keeping 60 ms (`--trim-keep-ms`). A slot counts as silent when its energy code is 15 codes
  (34 dB) or more under the message's 98th percentile, so the rule is relative to the talker,
  as the silent-frame study recommended. No K6 change. With the short last group: XL 12.2% fewer
  packets (2400), 18.0% (1200) on pre-trimmed corpus clips; real mic recordings gain more.
  `tools/k6_pcmhash.py` trims and packs the same way by default (same flags).
- **Pause frames** (`tx`/`send`, sotto 2400 and 1200, default; `--no-pause`): each run of 4 or
  more silent slots (the trim rule; in sotto 1200 the next slot's A energy must be quiet too)
  goes as its first slot plus one pause frame: voicing 0, Wo 127, energy 0, a 10-bit count N
  (bits 14-23) and the marker 0xA5304B (bits 24-47). A K6 from 2026-11-07 plays N slots of fill
  there, replaying the last real slot with its good flag (so a lost one is concealed, never a held
  speech frame). When replaying would leave the decoder's voicing state (prev_v, for code 3) other
  than the real run does, the run's last slot goes too. Older images play a pause frame as one
  silent slot (its sotto 1200 B half is unvoiced with energy step -6), so pauses come out short.
  `rx` plays pause frames out (`--no-expand` keeps them). The fill fades: each replay of the
  held slot carries an energy code 2 (sotto 2400, per 20 ms) or 4 (sotto 1200, per 40 ms) codes
  lower than the last, down to silence, so a pause is a short fade-out (about 27 dB in 120 ms)
  rather than a quiet 40 ms slot looping at 25 Hz, which Jeff heard on 2026-11-08 at the end of a
  sotto 1200 clip ("with... frequency tracking"). The radio and the PC twin (`expand_pauses`,
  `k6_pcmhash`) fade alike, so `pcm=` checks still match. All three together on 25 clips: XL
  19.0% fewer packets (2400), 21.5% (1200); L 15.5% / 15.0%.
- **Profile XL** (`--payload XL`, K6 key 2): 128-byte payload, the same header, 20 frames, no
  reserved bytes; groups of 8 packets carry 160 frames. In packet mode the preamble is 16 bytes
  before the first packet and 24 before the rest: after a 128-byte packet the K6's chip reports
  its end late, and with 16 bytes the K6 false-synced on the next one. On air (2026-11-04, K6
  build b0ff399, F/1200): 8/8, 0 bit errors; a 12 s clip is 33.4 s on air against 45.0 s in L.
- **Parity packets** (`--parity`, spec section 14, 2026-11-07): after each interleave group one
  more packet whose magic is the codec's | 0x10 (0xB7, 0xB9, 0xBC, 0xBD) and whose body is the
  XOR of the group's data packets' bodies. A K6 that lost one packet of the group rebuilds it
  exactly (its PLAY line counts them in `rebuilt=`; `missing=` lists only what stayed lost), so
  a single loss per group no longer costs concealed frames. Two lost in one group: both stay
  lost, as without parity. Costs 1/D more air time: 12.5 % at depth 8 (25 % at depth 4). The
  data packets are unchanged and older K6 images drop the parity packets as an unknown magic,
  so a parity send plays on any image. `modem.k6 rx` rebuilds the same way.
- **Stream framing** (`--stream`, K6 key 7 stream mode): after the lead-in, packets go back to
  back with only the sync word between them. The K6 finds the sync in software.
- **F at 2400 baud** (`--phy F --baud 2400`, K6 keys 1 and 5): 1200/2400 Hz tones, MSK. On air
  the K6 transmits it (the PC decodes it), but it never synced in receive (2026-11-04: six
  REG_58 values, levels 0.1-0.7, two tilts).
- **Profile S** (`--payload S`, 16 bytes) is still made here, but the K6 firmware no longer
  takes it.
- **Timing:** 150 ms of 0x55 after keying (`--lead-ms`; it was 400, but a long run of 0x55
  made the K6 false-sync on the first packet), carrier and 0x55 kept up between packets, 2
  bytes of 0x55 before unkeying.
- **Throwaway first packet** (`--prime`, off by default since K6 image 8d4be41): a copy of
  the first packet goes in front. Older K6 images re-arm on an idle RX-done with no sync about
  50 ms before the first sync word, too little preamble to lock, so the first packet failed in
  10 of 20 bursts; with the copy, 20 of 20. Image 8d4be41 doesn't re-arm there (idlekeep, on at
  power-on): 20 of 20 without the copy, and VE9QRP 500/500 as L and XL, female_libri198 600/600
  as XL (2026-11-05). Use `--prime` only with an older image.

| | Air time per packet | Speech per packet | 15 s message |
|---|---:|---:|---:|
| 1200 baud, profile L | 560 ms | 160 ms | 54 s |
| 2400 baud, profile L | 280 ms | 160 ms | 27 s |
| 1200 baud, profile S | 240 ms | 40 ms | 90 s |
| 1200 baud, XL (packet mode) | 1,050 ms | 400 ms | 42 s |
| 1200 baud, XL, stream | 880 ms | 400 ms | 35 s |
| 2400 baud, XL, stream | 440 ms | 400 ms | 18 s |

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

*2026-11-04, measured:* the IC-7100's USB audio in **FM-D** is much wider than a voice path, at
least on receive. Open-squelch discriminator noise on a clear channel, recorded from its USB codec,
is flat from 20 Hz to 2 kHz, 3 dB down at 4.5 kHz, 6 dB at 6 kHz and 10 dB at 8 kHz. That only
measures the receive chain. If the transmit chain is as flat, profile D could work over USB in FM-D
without DATA2. That is still untested: the first D bursts over USB got nothing, but that was
before the receiver's 64-byte fix. Retried on the fixed receiver the same night: D at 1200
baud over USB in FM-D delivered 8 of 16 test packets and 9 of 64 for a speech clip, with slips at
random points, while F delivered 63 of 64. D at 2400 got no sync. So over USB, **use F**. Profile F
over USB is what works on air (`docs/k6-receiver.md`, *On air*).

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
| FIFO words go high byte first | **Settled 2026-11-04: the chip sends each word low byte first.** The K6's first test burst (recorded on the IC-7100's USB audio) decoded bit-perfect in D and F once words were swapped; `rx` now accepts swapped packets and says so |
| The K6's own 2400-baud CAL burst decodes | **Settled 2026-11-04 09:28 UTC (image f190a46):** the IC-7100's recording decodes 2/2 in D/2400 (inverted) and 2/2 in F/2400 as test packet 0, no word swap needed. The 2.8 s cut is `tests/data/k6/air/k6cal-2400-20261104T0928.flac`, replayed through `rx` in CI (`tests/test_k6_air.py`). The first D/F 1200 recording isn't kept |
| The chip accepts 64-byte frames | **Settled 2026-11-04: yes, with timed FIFO reads** in the receiver (packet mode's FIFO interrupt can't keep up past 16 bytes). 0 bit errors in F |
| ±2.5 kHz deviation suits the K6's receiver | Steps 2-3 (match the K6's own transmitter) |
| The IC-7100's DATA2 9600 input is flat to ~20 Hz | `cal` / `fit`. Its USB audio in FM-D is flat 20 Hz-2 kHz on receive (measured 2026-11-04); transmit untested |
| Our demodulator is a fair stand-in for the chip | The first on-air tests |
