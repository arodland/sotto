# Coded 4FSK for the K6 pager (PHY G)

*2026-11-07. Sender `modem/k6g.py` (`python -m modem.k6 tx ... --phy G`). K6 receiver
`c/k6/ifpager.[ch]`, linked into the pager image and started by control action 10 (`ifrx`).
Tests `tests/test_k6_ifpager.py`. This follows the study in [k6-if-modem.md](k6-if-modem.md).*

## What it is

The pager's usual link is the FD6818's own FFSK modem at 1800 baud (PHY F). The chip makes hard
decisions, so any bit error costs the whole packet. PHY G carries the same XL packets (128 bytes,
whitened, the same CRC, interleave groups, parity packets and playback) a different way:

- 4FSK at 1200 baud, with tones at 900, 1500, 2100 and 2700 Hz, Gray coded, at 0.12 peak.
- A rate-1/2 K=7 convolutional code (polynomials 133 and 171).
- On the K6, the IF matched filter `ifrx.c` decides on GP0 code 13's stream, as the `iflive`
  modules do. Each tone's metric becomes a soft bit, and a soft Viterbi decoder recovers the
  packet.

The study put this at 41.1 dB-Hz for 1% bit errors, against 48.8 for uncoded FFSK through a
discriminator. On the real K6 captures, the live 4FSK floor is 2.4-2.6% raw bit errors, which the
code clears: the soft decoder over the real metrics made 0 errors in 24,000 bits.

## On the air

A burst is:

1. A control packet (F/1800, XL, two copies) with action 10. Byte 4 holds the session's longest
   time in units of 2 s.
2. 0.5 s of bare carrier.
3. A lead of 2400 symbols (2.0 s), tones 0 and 3 in turn, starting with 0. `ifrx` acquires on it.
   Its 256 symbols of lead steering end before the data, even when the lock comes 1.3 s in.
4. For each packet, a sync word of 24 symbols (`100100000111111001101010`, 1 = tone 3,
   0 = tone 0), then 1032 coded symbols. No window of the lead running into the sync word is
   closer than 10 symbols to it. The receiver takes a window with up to 6 wrong.
5. 48 more symbols of the lead's pattern.

A packet's coding is as follows. Its 128 bytes, MSB first, are followed by 8 zero bits: the
code's 6 tail bits and 2 more. That gives 1032 trellis steps and 2064 coded bits. They are
interleaved in 24 blocks of 86 bits. Symbol i of block b carries coded bit 86b + i as its Gray
value's high bit and 86b + 43 + i as its low bit, so a symbol's two bits are 21 trellis steps
apart.

One packet takes 1056 symbols (0.88 s) and carries 20 sotto 1200 slots (0.8 s of speech). So
PHY G runs at 0.91x real time. **A 12 s clip** (15 XL packets) takes 15.2 s for the burst and
17.3 s with the control packet. At F/1800 the same clip takes 10.8 s. PHY G trades air time for
margin. The faster 4FSK is the next step (below).

## On the K6

`ifp_session` (c/k6/ifpager.c) runs from the pager's main loop once a control packet asks for
it:

- It sets up GP0 code 13 on PB1 and DMA channel 1, the same way iflive does, and reaches the RF
  chip through the module API (receive only).
- It uses the PWM ring as its DMA ring, so nothing can play during the session.
- It hands each decoded packet to `sp_packet`.
- It ends when no sync word has come for two packets' time, when a key is pressed, or at the
  control packet's limit.
- The pager then plays the message as it would any other.

It prints two lines:

    K6 IFP ret=15 ms=16930 syncs=15 syms=20510 lost=0 q=88 rej=0 afc=-160 idle=43 ks=5490 stack_free=1210
    K6 IFP pkts=15 r=222111111111113 miss=000100000000000

- `ret` is the number of packets handed over: -2 a key, -6 no lock.
- `lost` is the counts lost to ring overruns.
- `q` is the lead fit, out of 256.
- `afc` is the AFC offset in Hz.
- `idle` is the CPU idle percentage.
- `r` gives each packet's `sp_packet` result: 0 dup, 1 added, 2 new, 3 complete, C a bad CRC.
- `miss` gives each sync word's wrong symbols.

The decoder (`ifp_sym`, `ifp_work`) works like this:

- `ifp_sym` takes each symbol's metrics. It turns them into two max-log LLRs scaled by a
  running mean of the winning metric (32 for a symbol as strong as the mean, clipped at
  ±127), writes them into the interleave block that is filling, and checks every decision
  against the sync word.
- `ifp_work` runs one trellis step (about 30 us on the M0) whenever a block is in. It uses
  int16 path metrics, 64 decision bits a step, and a sliding traceback of depth 64 that
  decides 16 bits at a time in a ring of 80 steps (640 B). The terminated tail gives the last
  traceback.
- The session runs these steps, and the packet check, only in the gaps between the receiver's
  bursts (half way through a symbol, and at its end, about 120 us each). Started late, they
  would overrun the 183 us DMA ring.
- `sotto_pager.c`'s CRC now goes a nibble at a time. It is about 4x the speed (about 30 us a
  packet), with the same results.

Stack: about 2.9 KB in the session (the emulator measures it), under `poll`. The lr image
leaves about 1 KB to spare. Flash: about 5 KB (the receiver, its K6-trained templates and the
decoder). The lr image keeps 18.7 KB free. The lean images don't call it, so it isn't linked
there.

## Results so far (before the bench)

| Test | Result |
|---|---|
| The code, the K6's integer decoder, through metric noise at 5.8% symbol errors (the live floor is 4-6%) | every packet; the float reference receiver too |
| The whole receiver on the GP0 model, model templates | every packet down to 43 dB-Hz; 41 dB-Hz fails (9-58 bit errors a packet, in float too) |
| The same, K6-trained templates (as the image) | every packet at 46-47 dB-Hz, ±164 Hz; on the model they lose 1-2 dB, on the K6 they win |
| A symbol lost mid-packet | that packet only; the next sync word is still found where it is |
| The session on the emulated M0, 8 packets at 55 dB-Hz, -164 Hz | 8 of 8 in order, 0 counts lost, 43% idle, stack 2.9 KB |

The model's 4FSK distorts at 0.15 peak and up, so the sender's default is 0.12, as for the live
iflive runs. The real IC-7100 limits its deviation past about that level.

## On the bench

The K6 receives and the IC-7100 sends, on 440.850 MHz with the CW ID. Flash the CI image
`aura-pager-lr.bin`, keyed with `modem.k6 keyimage` as usual. Then:

    python3 -m modem.k6 tx CLIP.sot g.wav --phy G --payload XL --codec 1200 [--parity] --cw-id "AG7EW SOTTO TEST"
    sh tools/k6_send.sh g.wav

On the serial log, look for `K6 CTL seq=.. ifrx`, the two `K6 IFP` lines, and the usual `PLAY`
line, whose pcm hash `tools/k6_pcmhash.py --check` compares as for F. A message has to fit the
store: 25.6 s of sotto 1200. The session stores the whole message, then plays it.

## Next: a faster 4FSK

PHY G at rate 1/2 carries 1200 bit/s. The built-in FFSK nets about 1500 bit/s after packet
overhead. Ways to get ahead of it, roughly in order of cost:

- **Puncture to rate 2/3** (keep 3 of every 4 coded bits). That gives 1600 bit/s at 1200 baud,
  at about 1.5-2 dB less margin than rate 1/2. It needs only the sender's pattern and erasures
  (LLR 0) in the decoder, with no CPU change. With a shorter lead and sync, a 12 s clip would
  take about 11 s.
- **A faster symbol rate.** 1600 or 1800 baud 4FSK, with the tones re-spaced to fit the IC-7100's
  3.1 kHz passband. That needs new templates (TL, taps) and costs CPU at the K6's margin; MSK 2400
  already failed on this transmitter.
- **Play while receiving** (stream mode), so a long message starts after its first interleave
  group, and its length is no longer limited by the store. That needs the decoder to share the
  CPU with the receiver: the yield hook, or decoding in the receiver's idle time.
