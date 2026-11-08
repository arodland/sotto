# DMR on the K6: detect it, mute it, maybe decode it later

*2026-11-08.* Jeff's idea: the Baofeng UV-K6 should at least recognise DMR and mute its audio,
and perhaps decode it later. The local DMR repeater is on 440.925 MHz. Mixed-mode, it also sends
an analog CW ID, which EXPLORE keeps finding at -90 to -100 dBm.

**Answer: yes, with no hardware change, as a RAM module on today's image. It works on the air
(2026-11-08 07:02Z): the K6 muted a real DMR key-up on 440.925 20 ms after the carrier came up,
then played the repeater's analog CW ID** (see "Run 3" below; the first design needed the gain
frozen and a band-pass added before it saw real DMR). The K6 can't hear
its own receive audio: the FD6818's AF output goes straight to the speaker amplifier. But it
can see the receive IF on PB1 (GP0 code 13, the sigma-delta stream that `gp0dma` and the FT8
and softmodem modules read by DMA). In that IF's FM-demodulated frequency, DMR's 48-bit sync
words show up plainly. A correlator for them, about 3 KB of Thumb code using about a quarter of
the K6's CPU, finds every kind of DMR transmission in the model within 0.1 to
0.7 s. It found nothing in 30 s each of FM voice, FFSK, CW tones and noise. The `dmrdet` module
mutes the speaker (REG_47) while DMR lasts and plays FM between. A later decode can reach the
metadata (colour code, talkgroup, source ID), but not the AMBE+2 voice.

```
FD6818 low IF (16.93 kHz), gain frozen at REG_13 0x03FF -> GP0 code 13, 1-bit sigma-delta on PB1
  -> DMA ch1, 5.5 MS/s, 512 B ring -> ones per 64 samples (85.8 kHz counts)
  -> band-pass biquad around the IF -> rising zero crossings, interpolated
  -> the IF's phase at 9600 points a second (2 a DMR symbol), the grid set from the DMA's measured rate
  -> frequency per grid step -> spread gate -> 4 sync correlators (BS, MS, direct TS1, TS2)
  -> hit (rho >= 0.8) -> confirmed (2 hits a whole number of 30 ms slots apart) -> mute REG_47
```

Code: `sim/k6if/dmr.py` (the float model and the study), `c/k6/dmrsync.h` (the integer detector,
header-only), `c/k6/dmrsync_host.c` (it on the host), `c/k6/mod/dmrdet.c` (the module), and
`tests/test_k6_dmr.py`.

## What DMR looks like to the K6

DMR (ETSI TS 102 361-1) is 4FSK at 4800 symbols/s. Dibits 01, 00, 10 and 11 map to +3, +1, -1
and -3 units of 648 Hz deviation, with root-raised-cosine pulses (alpha 0.2), in two 30 ms TDMA
slots. Each burst is 54 payload symbols, a 24-symbol mid field (sync, or embedded signalling),
then 54 payload symbols.

- **A repeater (BS) transmits continuously while it's up.** Both slots go out, each with a
  12-symbol CACH in front, and an idle slot carries idle bursts with a data sync every 60 ms. So
  at a repeater's output the carrier does **not** pulse. The 16.7 Hz RSSI pattern is real only
  for a mobile or a direct-mode radio (one 27.5 ms burst every 60 ms), so RSSI alone would miss
  the repeater, which is the case that matters here.
- **The sync words use only the outer symbols (+-1944 Hz) and are balanced.** A data sync is
  the voice sync negated, so one correlator per source family finds both, and the sign tells
  which. A voice call carries its sync in one burst of six (every 360 ms a slot). Headers, data,
  CSBK and idle bursts carry it in every burst.

| Family | Voice sync | Data sync |
|---|---|---|
| BS (repeater output) | 755FD7DF75F7 | DFF57D75DF5D |
| MS (mobile to repeater) | 7F7D5DD57DFD | D5D7F77FD757 |
| Direct mode, TS1 | 5D577F7757FF | F7FDD5DDFD55 |
| Direct mode, TS2 | 7DFFD5F55D5F | D7557F5FF7F5 |

**What the K6 can't use.** The MCU gets no audio samples (`docs/ht-survey.md`). The chip's FSK
modem tops out at 2400 baud, binary only. The coordinator's spectral-flatness idea needs audio
samples too; the discriminator frequency from GP0 is that audio, and the sync words in it
are a sharper test than flatness.

## The detector

1. **Discriminator.** Rising zero crossings of the 85.8 kHz counts, interpolated (as `gp0dma`),
   give the IF's phase at each crossing. Linear interpolation gives the phase at a uniform grid
   of 9600 points a second. Each grid step's phase change, less the nominal IF's, is the
   frequency over that 104 us (Q11, 4.69 Hz an LSB). That is an integrate-and-dump over half a
   symbol; the model found no gain from 4 points a symbol or full-symbol windows.
2. **Grid timing.** The grid's step in counts comes from the DMA's rate, which moves about 3%
   with code placement and bus load. The module measures it against TIM17 every ~90 ms
   (`ks=` on the summary line). A grid 1% off still gives hits, but their spacing drifts off
   the 30 ms slot raster; 0.3% off is fine (the confirmation allows 1/256 of the gap).
3. **Spread gate.** Over the sync's 24 symbols (every other grid value), D = 24 sum(y^2) -
   (sum y)^2 is 576 times the variance. A window whose spread is outside 1300-2600 Hz rms is
   skipped: a bare carrier, squelch noise (far wider), most voice. The outer-symbol sync is
   1944 Hz rms.
4. **Correlators.** For each family, c = sum p_i y_i, computed as 2 x (the sum over the
   pattern's 12 plus positions) - sum y, and rho^2 = c^2 / D: a normalised correlation, so it
   ignores the carrier's frequency offset and the deviation's scale. A hit is rho >= 0.8,
   the best within +-2 grid points.
5. **Confirmation.** A hit is confirmed when its family had another hit a whole number of
   slots (1 to 24, up to 720 ms) earlier, within +-2 grid points plus 1/256 of the gap. All DMR
   bursts sit on the 30 ms raster, so this holds for repeater, mobile and direct mode alike.

## Results in the model

The model is the GP0 path from the softmodem study (`sim/k6if/frontend.py`: the FD6818's filter
and AGC, the 1-bit sigma-delta, the DMA at 5.49 MS/s), with DMR from a TS 102 361-1 framer
(`sim/k6if/dmr.py`: CACH, bursts, superframes, RRC alpha 0.2), 3 s per case.

| Case (C/N0 54-70 dB-Hz) | Syncs found | Confirmed | First confirm after the carrier |
|---|---:|---:|---:|
| Repeater: a voice call joined midway + idle slot | 58 | 57 | 0.11 s |
| Repeater: voice calls on both slots | 18 | 16 | 0.14 s |
| Mobile: voice joined midway (pulsed carrier) | 9 | 7 | 0.50 s |
| Direct mode TS1: voice joined midway | 8 | 7 | 0.62 s |
| FM voice (3 speakers, CTCSS), FFSK 1200, a keyed 1 kHz tone, a bare carrier, noise | 0 | 0 | |

- **Sensitivity:** unchanged down to 54 dB-Hz. At 50 dB-Hz about a third of the syncs still
  come through, and they die out at 47, where the zero-crossing discriminator passes the FM
  threshold. 600 Hz of carrier offset changes nothing. (This section first said 54 dB-Hz was
  about -116 dBm with an 8 dB noise figure. On the K6 it isn't: with the pager's AGC, the real
  repeater at -85 to -90 dBm reached GP0 at what the model calls about 40 dB-Hz. See "Captures
  of real DMR" below.)
- **False hits:** over 30 s each at 55 dB-Hz, the largest |rho| was 0.63-0.75 on FM voice,
  0.76 on FFSK, 0.69 on the keyed tone and 0.53 on noise (the threshold is 0.8). Random 4FSK
  at 4800 baud (what YSF or P25 payload looks like) gave 4 single hits in 30 s and no
  confirmation. A false confirmation would need a second such hit within a few grid points
  of a slot multiple: about one every few hours of continuous 4FSK. Muting YSF or P25 would be
  welcome anyway.
- **The C core matches the float model** on DMR, FM voice and FFSK: the same confirmations
  within 10%, the same families, nothing on non-DMR (`test_c_matches_python`).

## The `dmrdet` module

`c/k6/mod/dmrdet.c`, API level 2 (it uses `key_poll` on level 4 images), receive only. It tunes
to 440.925 MHz (wide), puts GP0 on code 13, runs DMA channel 1 into a 512-byte ring (TIM17 as
the clock), and plays the receive audio on the speaker (REG_47 AF 1, PB2 on) only while
there is a carrier (RSSI 10 dB over the floor measured at the start) and no DMR. The audio
opens 150 ms after the carrier comes up, so a transmission's first syncs (its headers, or
the repeater's idle slot) keep it shut. Once DMR is confirmed, any sync of that family keeps it
muted for 720 ms more, enough to bridge a voice superframe's 360 ms. It runs 600 s; any key
stops it. Everything it changes is put back.

```
dmr start f=440925000 floor=-125
D on bs d r2=234 +13          DMR: family, voice/data sync, rho^2 x 256, ms after the carrier
s 60 h=15 lost=0 idle=58 ks=5490
c -60 2578 h=55               a carrier came and went: dBm, ms, syncs during it
D off 2611
```

On the emulated Cortex-M0 (`tests/k6_live_emu.py`'s CPU and DMA model, calibrated against
`gp0dma` on Jeff's K6) against the modelled stream:

- **A repeater keying up with a voice call:** DMR confirmed 13 ms after the module saw the
  carrier, inside the 150 ms hold, so the speaker never opened. No samples lost, CPU idle
  54-58% (busy 42-46%), the DMA rate tracked to 5490 kS/s.
- **FM voice:** no DMR, the audio opened after 150 ms, one `c` line with `h=0`.
- **Size:** 3104 bytes of code (CI's clang makes it ~90 bytes bigger). The 512-byte ring and
  the state are on the stack (see "Captures of real DMR"). The ring is 512 bytes (93 us) rather
  than gp0dma's 1 KB because the module needs the room. That's why it checks keys with
  `key_poll` (5 us) rather than `key_down` (60 us), and makes only one FD6818 call at a time
  between drains. Getting it to fit cost the screen (the WSPR modules' level 6 calls) and
  `gp0dma`'s 20 ms rate measurement at the start: the grid starts at 5.49 MS/s and is
  corrected within ~90 ms.

## How it fits EXPLORE and the listener

- **EXPLORE: built (2026-11-08, `c/k6/xdmr.c`, docs/k6-explore-app.md "DMR mute and the CTCSS
  read").** Every listen runs the detector alongside the CW ID listener, mutes DMR on the
  speaker, marks the row DMR and sets the saved entry's `RT_F_DMR`. The plan was:
  when it listens to a row, run the detector
  alongside the CW ID listener. The CW ID decoder uses the FD6818's tone detector, not the
  audio, so muting doesn't affect it. A row that confirms DMR gets a DMR mark, and its
  table record a mode flag and later the colour code (byte 53 is spare,
  `docs/k6-repeater-table.md`). The 440.925 row that EXPLORE keeps listening to without decoding anything would
  then say "DMR" at once, and listen only for the analog ID.
- **Aura's own receiver:** the same detector could gate the speaker in normal VFO and memory
  listening. That needs firmware: the DMA running whenever the squelch is open, and ~45% of the
  CPU while a DMR carrier lasts (less on FM: the correlators run only on windows that pass the
  spread gate). It would go into the pager patch, like EXPLORE, after the module has proved
  it on air.

## Decoding DMR metadata (2026-11-08)

Metadata only: colour code, slot, talkgroup or unit, source ID, talker alias, and which CSBKs and
data packets go by. No voice: DMR voice is AMBE+2, which is patent-encumbered. The open decoders
(mbelib, and OpenDMR's voice path built on it) are PC code of uncertain legal standing that would
not fit the M0 anyway.

**Python reference (`sim/k6if/dmrdec.py`).**
- It reads a receiver's FM audio (`audio`, `calls`, `summary`, `check`) or the K6's GP0 counts
  (`counts_symbols`), resampled to 8 points a symbol through an RRC matched filter.
- The input's polarity comes from the slot types (data syncs must give clean Golay words). The
  IC-7100's audio is inverted.
- Each burst decodes as follows:
  - TACT: Hamming (7,4).
  - Slot type: Golay (20,8), the (23,12) generator 0xC75 shortened, plus overall parity.
  - Voice LC headers and terminators: BPTC (196,96) and RS (12,9), parity masks 0x96 and 0x99.
  - CSBKs and data headers: CRC-CCITT, init 0, inverted, masks 0xA5A5 and 0xCCCC.
  - Voice bursts B-E: the EMB, QR (16,7,6) from the (17,9) QR code's generator 0x139, and the
    embedded LC, BPTC (128,72) with a 5-bit checksum.
  - Talker alias: FLCO 4-7.
- A call's headers and terminators are also soft-combined. A terminator's masks are turned
  into a header's, which works because the codes are linear.
- `voice_call_symbols` builds whole calls for the tests.
- `tools/k6_dmrwatch.py` turns recordings into a JSONL answer key, one line per call or data
  packet.

**Checked on 440.925 (11 IC-7100 recordings).**
- Every layout came out at zero syndromes. Everything is colour code 1.
- Voice is on TG 3027 (FLCO 0, FID 0x10) from three radio IDs.
- Every ~7 s burst is a DMR GPS report: 5 preamble CSBKs, an unconfirmed data header (SAP 3)
  and 8 rate-1/2 blocks carrying `$GPRMC`, relayed over the network from distant stations.
- No talker alias was seen.

**On the K6 (`c/k6/mod/dmrrx.c`).** It is dmrdet with the decoder (`c/k6/mod/dmrdec.inc`,
`c/k6/dmrburst.h`). It keeps the mute.
- dmrsync.h's 2-point-a-symbol grid feeds a 13-tap RRC whose output is kept in an 8-bit ring of
  512 in the module's scratch.
- At each sync hit, the burst is read back:
  - The timing is found again to a quarter grid point on the filtered sync: the correlation at
    the 5 whole grid points around the hit, then a parabola through the peak, rounded to a
    quarter by comparisons. Reading between grid points uses Catmull-Rom. At 2 points a
    symbol, the nearest point alone left 1-2 bit errors a burst at any level.
  - The burst is scaled by its own sync.
  - The slot type and the CACH's TACT are read first. The rest of a header, terminator, CSBK
    or data header is sliced on the next poll, so the DMA ring drains between the two.
- Slot: every burst's TACT counts, voice included. Hamming (7,4) fixes one bit, and a fixed
  TACT is kept only if it agrees with the slots' alternation (every 288 grid points). A burst
  whose own TACT failed takes its slot from the last good one.
- Voice bursts B-E are read at A + 576 k grid points. A flywheel reads the next superframe
  360 ms on when its sync is missed (two in a row at most). Once a chain has passed an EMB
  check, voice hits off its beat are dropped: false hits had started chains of their own.
- Embedded LC: the four fragments are kept as soft bits, and taken when at least 2 of the 4
  EMBs check out. One row with two errors is fixed from the columns whose parity fails. A
  fragment set that fails alone is added to the last superframe's, then the one before (talker
  alias LCs come between the call's LC), then both: a call's LC is the same each superframe.
- The grid's step locks to DMR's 30 ms slots. The DMA's measured rate is only good to ~2%,
  which over a superframe is 10 symbols. The first measurement (~50 ms in) is taken whole,
  later ones averaged: the old 1/4 steps from the 5.49 MS/s guess cost most embedded LCs when
  the DMA ran 1% off it.
- Housekeeping (keys, RSSI, the screen, a line) waits, up to twice its 48 ms, for the other
  slot's time while a voice chain runs. Its API calls stop the counting and the filter fills
  again after them, ~5 ms lost, and on the radio they outlast the DMA ring. Emulated at 52
  dB-Hz with 300 us register reads: embedded LCs 16 to 24 of 32, full aliases 3 to 12 of 16.
- The grid index counts across pauses (`ds_skip`, laps from the clock), so bursts can still be
  predicted by it.
- Every line ends in ` @` and the module's time in tenths of a second, for
  `tools/k6_dmrscore.py`.

On the K6 model (`tests/test_k6_dmrdec.py`), 10 calls of 6 superframes for each level. The
"before" column is the decoder as first pushed:

| | before | now |
|---|---|---|
| 52 dB-Hz, embedded LC / full alias | 49 of 60 / 5 of 10 | 56 / 9 |
| 50 dB-Hz, embedded LC / full alias | 22 / 0 | 44 / 4 |
| 48 dB-Hz, voice mid bursts read / embedded LC | 146 of 240 / 1 | 188 / 11 |
| 48 dB-Hz, no alias (as on 440.925), embedded LC | 1 | 31 |

No run gave a wrong talkgroup, source or alias. The LC header itself (soft-combined) decodes in
every call down to 48 dB-Hz.

- **The DMA ring:** the emulated call lost the ring 34 times in 2 s. A gap trace (the DMA
  counter's reads, and what ran between them) put 45 of the 62 long gaps in the old quarter-step
  sync search. With the parabola, one alignment a poll, Hamming fixes found from the masks'
  columns, and BPTC stopping after a clean pass, it loses 3, all in housekeeping (the status
  line and an LC decode). dmrdet alone loses none. Since the first bench run the ring is 512
  bytes, in scratch: it laps ~20 times a second while a burst is read, which costs nothing
  (the burst is in the filter's ring by then; the same lines as with 1 KB at 50-52 dB-Hz), and
  housekeeping's laps aren't counted as lost.

| | |
|---|---|
| Code | 9.4 KB, so only the SPI voice images' 11,776-byte shared module block (PR #146); not EXPLORE or the lean images |
| Scratch | the filter's 512-byte ring, the decoder's 1,032-byte state and the 512-byte DMA ring (2,056 B), or it returns -6 and says `dmr scratch N need M`: never on the stack |
| Stack | 1,288 B emulated, of the 3,112 B the module call gets on the vlrh image |

Serial lines, besides dmrdet's (the summary line gains `L=` and `E=` counts, and every line ends
in ` @` and tenths of a second since the start):

    L TS1 CC1 TG 3027 src 3025107 F0 @52     voice LC header or terminator (comb N: only the soft sum decoded)
    E TS1 TG 3027 src 3025107 @131           embedded LC, when it says something new (late entry)
    A AG7EW TEST @120                        talker alias so far
    K TS1 bd00800104c2bf301cd5 @803          a CSBK that passed its CRC (here a preamble)
    H TS1 ... @804                           a data header

The screen (level 6) shows "DMR CC1 TS1", "TG 3027", "ID 3025107" and the alias.

**First bench run (16:43-16:53Z 2026-11-08, main bf7990a vlrh image, quiet repeater).** The
module ran its 10 minutes and returned 1, then the K6 reported `stack_free=0`, keyed for its
upload report and hung with the carrier up on 440.850 (low power) until Jeff cut the power at
~17:09Z. The module's stack had overrun the core's RAM: 2,580 B emulated (the 1 KB DMA ring and
two 288-byte slicing buffers on it) of the 3,112 B, plus the firmware's own frames inside the
API calls, which the emulator doesn't count. Also: `lost` climbed ~21 a second with no DMR on
the air (every housekeeping: the radio's API calls outlast the 1 KB ring, which the emulator's
20 us register reads never did; reproduced at 300 us) and `ks` never left its 5490 start (each
of those laps restarted the ~90 ms rate measurement). Fixed after: everything in scratch, the
stack at 1,288 B, housekeeping's laps not counted and the rate measured over each ~45 ms
stretch between them. The repeater was quiet (at most 11 syncs a summary), so nothing decoded yet.

**Scoring a bench run.** On hamlet, `tools/k6_dmrwatch.py` turns the IC-7100's 440.925
recordings into the answer key. `tools/k6_dmrscore.py --key KEY.jsonl --log K6.log` then lines
the K6's serial lines up with it: it finds the module's start from the talkgroup lines
themselves, or takes `--start`. It counts calls named and named right, wrong and stray lines,
data headers, and full aliases. It prints no payloads, which may hold people's positions.

## Real DMR from 440.925 (IC-7100 audio, 2026-11-08)

Five carriers recorded on the IC-7100 (FM-D, 12 kHz, 48 kHz USB audio, flat to a sharp edge near
2.5 kHz) between 04:24 and 04:37 UTC, run through `python3 -m sim.k6if.dmr audio` (the audio
stands in for the discriminator):

| Clip (UTC) | Length | Syncs (BS family) | Confirmed | First confirm | median rho at hits |
|---|---:|---:|---:|---:|---:|
| 04:24:56 | 8.9 s | 232 + 1 other | 231 | 1.04 s | 0.99 |
| 04:32:15 | 12.0 s | 304 | 303 | 1.05 s | 0.95 |
| 04:34:34 | 11.9 s | 284 | 283 | 1.05 s | 0.99 |
| 04:35:03 | 10.7 s | 261 | 260 | 1.05 s | 0.97 |
| 04:36:54 | 9.0 s | 232 + 1 other | 231 | 1.05 s | 0.97 |

- Every clip starts with about 1 s of margin, so DMR was confirmed within ~50 ms of the
  carrier, on its second burst.
- Syncs came every 30 ms (one slot apart) with a few two-slot gaps: both slots carried
  data, idle or header bursts most of the time. The rarer opposite-sign syncs are the voice
  bursts. The IC-7100's audio is inverted relative to the model (its data syncs read as
  voice), which the correlator doesn't mind.
- The family is BS throughout (a repeater), as expected; the 2 other-family hits were
  unconfirmed.
- The rest of the time |rho| stayed at p99 0.62-0.65, well under the 0.8 threshold.

So the sync shape, timing and framing hold on the real repeater, through a 2.5 kHz audio path
that's worse than the K6's IF. What's still untested is the K6's own GP0 discriminator on a real
DMR carrier: that's the bench run.

## On the K6 (2026-11-08)

Two receive-only runs of CI's `mod-dmrdet.bin` (3116 B with header, from main f56f0a0) on
440.925, the IC-7100 sending the upload on 440.850:

- **Run 1 (05:08:55Z, no cable, speaker on):** Jeff heard a DMR transmission start as a short
  buzz, then a prompt mute, at about 05:17:40, and the analog side otherwise behaved. The report
  went out on 440.850 (Jeff saw the TX LED on time) but the IC-7100 was back on 440.850 about
  5 s too late: the K6 starts a module as soon as it has the last new chunk, ~17 s before the
  upload audio ends, so the hamlet side now returns 35 s earlier. No log from this run.
- **Run 2 (05:39:12Z, cable in, serial log):** a miss. Floor -108 dBm, one 0.5 s carrier at
  -91 dBm (05:47:15, `c -91 488 h=0`): not one sync hit, yet the IC-7100's recording of it holds
  16 BS syncs one slot apart at rho 0.97 (`audio` mode, 15 confirmations from 1.24 s). One stray
  hit early on never confirmed, `lost=0` every minute, `ks=5374` (the real DMA rate, 2% under
  the model's 5490; a 2% grid error would cost the confirmations, not the hits, so it isn't the
  rate). 17 dB over the floor is about 58 dB-Hz, where the model finds every sync. So the K6's
  own IF path doesn't look like the model on real DMR, and the mute Jeff heard in run 1 is
  unconfirmed (the IC-7100's copy of 05:17:40 is bare hiss).
- **CPU idle:** the run printed `idle=286` and then 1-2%, which was a reporting bug: the idle
  and window sums were in 96 MHz cycles and wrap a u32 every 44.7 s, so a 60 s window can't be
  measured. Unwrapping the numbers gives about 73% idle in the first minute and 75% after that,
  better than the emulator's 54-58% (its CPI 1.5 is pessimistic). The summary line now comes
  every 40 s, inside the wrap (summing in coarser units cost 32 bytes the store doesn't have
  with CI's compiler).
- **Run time:** the firmware's `K6 MOD ret=0 in 3766 ms` is also wrong (600 s ran). Its
  module clock only advances when the module calls into the firmware's tick (delays, TX
  guard), and the 100 us counter wraps every 6.5 s between calls; dmrdet only polls keys and
  RSSI. Cosmetic, firmware side.
- **The short buzz:** the speaker opens 150 ms after the carrier (`HOLD_MS`), and a confirm
  needs two syncs a whole number of slots apart. On a repeater that is keying up from idle the
  second sync can come later than 150 ms. If that buzz is common, raise `HOLD_MS` (FM pays it
  as opening delay) or open on a single strong sync's absence for 2 slots.

- **Run 3 (06:54:46Z, cable in, PR #140's module: gain frozen, band-pass):** it works. The
  only carrier in ten minutes was the repeater keying up at 07:02:35Z with ~0.5 s of DMR (the
  IC-7100's audio: 17 BS syncs at rho 0.94), then dropping to analog for WA7DMR's CW ID
  (975 Hz, 20 wpm, ~3.7 s). The K6, verbatim:

  ```
  07:02:35Z D on bs d r2=198 +20      DMR (BS, data sync, rho 0.88) 20 ms after the carrier
  07:02:36Z c -80 483 h=14            the DMR carrier: 14 syncs in 483 ms
  07:02:36Z D off 559
  07:02:40Z c -80 3740 h=0            the analog ID: no syncs, speaker open
  s 40..560 h=0..15 lost=0 idle=73-74 ks=5390-5393
  ```

  Muted inside the 150 ms hold, so the DMR was never heard. The analog ID played. No false
  `D on` in the other 9.5 minutes, no counts lost, 74% idle (the band-pass costs ~1%). The
  floor reads -97 dBm at the frozen gain (-108 under the AGC).

### Captures of real DMR (2026-11-08 06:06Z)

`c/k6/mod/dmrcap.c` (level 2, receive only) listens on 440.925 and, each time the RSSI rises
8 dB over the floor, records 38 ms stretches of the raw IF counts (on the stack, the first
within ~5 ms of the carrier) and dumps them over the cable in gp0cap's format, plus one
no-carrier reference. A repeater with both slots going has a sync every 30 ms, so each capture
holds a whole one. `python3 -m sim.k6if.dmr cap LOG` runs the detector on every capture (IF
offset, best sync with and without the spread gate, measured +-3 deviation), and `--diag`
compares four discriminators and measures how far the IF band stands above the 1-bit path's
noise.

The first run (pager's AGC only) caught two real transmissions, three captures each, which the
IC-7100's audio decodes at rho 0.98-1.00:

| | counts rms | IF band over the noise above | best \|rho\|: MCU crossings / band-pass / complex | IF mean, spread (MCU crossings) |
|---|---|---|---|---|
| no carrier (-108 dBm) | 12.8 | -16 dB | 0.77 / 0.67 / 0.68 | noise |
| DMR, -85 to -90 dBm | 4.1-4.4 | +3.4 to +5.3 dB | 0.64-0.76 / 0.59-0.89 / 0.67-0.86 | +2.5 to +3.5 kHz, ~5 kHz |
| model at 60 dB-Hz | 21 | +20 dB | 0.99 / 0.98 / 0.98 | +0.1 kHz, 1.4 kHz |
| model at 42 dB-Hz | 5.7 | +7 dB | 0.66 / 0.72 / 0.60 | +1.8 kHz, 4.8 kHz |

The real DMR looks like the model at about 40 dB-Hz. The detector isn't wrong: the IF reaches
GP0 too weak. With no carrier the counts swing at rms 12.8 (the receiver's noise at full
gain). With the -85 dBm carrier they drop to 4.2, about a fifth of the model's working level,
so the AGC turns the IF down to where the sigma-delta's own noise is only 4-5 dB below it in
band. Even a band-pass ahead of the crossings, or a complex discriminator, only reaches
0.8-0.9 on half the captures. (The pager's 1200-baud modes have far more margin per symbol,
which is why they never showed this.)

The second run (06:31Z) cycled each carrier's captures through four gain steps: the pager's
AGC, then the AGC frozen (REG_7E bit 15, index 3) with REG_13 = 0x03FF, 0x03BE, 0x037E. Two
7 s DMR transmissions, eight captures each:

| gain | RSSI reads | counts rms | IF band over the noise | best \|rho\|: bare crossings / band-pass / complex |
|---|---|---|---|---|
| pager's AGC | -86 | 4.3 | +4 to +5 dB | 0.60-0.69 / 0.65-0.94 / 0.67-0.95 |
| REG_13 0x03FF | -79 | 6.8 | +10 dB | 0.71-0.82 / **0.95-0.99** / 0.90-0.97 |
| REG_13 0x03BE | -86 | 4.3 | +4 to +5 dB | 0.59-0.70 / 0.75-0.88 / 0.68-0.90 |
| REG_13 0x037E | -94 | 3.5 | +1 dB | 0.60-0.71 / 0.60-0.76 / 0.65-0.72 |

At full gain the real DMR matches the model at about 48 dB-Hz (+10 dB, bare crossings 0.80,
band-passed 0.95). Bare crossings still miss most syncs there; band-passed ones find them.
So dmrdet now does both:

- **Gain:** it freezes REG_13 at `GAIN13` (0x03FF) for the run and puts the AGC back after.
  The RSSI reads ~7 dB higher at that gain, and the floor is measured after the freeze.
- **Band-pass:** `dmrsync.h` runs each count through a 2nd-order Butterworth biquad around the
  IF (10.4-23.4 kHz at 85.8 kHz, Q14 coefficients, Q8 out) before the crossings. That's 3
  multiplies a count. On the model it moves the threshold ~3 dB: at 48 dB-Hz, 30 of 39 syncs
  and 29 confirmations instead of 3 and 1. At 46 dB-Hz it's 11/10 instead of 0. A 4th-order
  filter adds ~1 dB more. False hits are still nil (10 s each at 46 and 55 dB-Hz of FM voice,
  FFSK, a keyed tone, a bare carrier and noise; the largest non-DMR |rho| is 0.77, on FFSK).
- **Room:** the 512-byte ring and the 452-byte state moved to the module call's stack (the
  emulator measures under 2 KB in all, against 3.5-6 KB), so the store only has to hold the
  code: 3104 bytes.

To rerun a capture: cable in, `tools/k6_upload.py mod-dmrcap.bin --api-level 2 --run-s 620`
while 440.925 is busy, with the IC-7100 recording alongside for ground truth.

## Bench plan (receive only; done, kept for reruns)

1. Real DMR first: the explorer thread's hamlet recorder rotates through 440.925 and is cutting
   DMR and analog clips into `~/sotto-work/clips/dmr-real/`. The IC-7100's FM audio is
   de-emphasised and band-limited (300-3000 Hz), so the clips check timing and framing, not
   the K6's discriminator. A flat (9600 bps DATA) output would be better if the rig offers one
   over USB.
2. On the K6: from EXPLORE press EXIT twice to VOICE MSG, then upload CI's `mod-dmrdet.bin`
   (`tools/k6_upload.py mod-dmrdet.bin --api-level 2 --telemetry 12 --run-s 620`). Telemetry
   brings the first ~20 lines back over the air without the serial cable, which mutes the
   speaker. The K6's report goes out on 440.850 with its MCW ID, inside the bench rules. The
   module listens on 440.925 for 10 minutes with the speaker on, and the IC-7100 records
   440.925 alongside as ground truth. Jeff should hear the repeater's analog ID and FM, and no
   DMR buzz. A second run with the cable in gives the full serial log.
3. What would change the plan: syncs found but no confirmations (the grid rate, then the
   tolerance); no syncs at all on a strong DMR carrier (the narrow/wide filter, the deviation,
   or the GP0 path under the FD6818's AGC); `lost=` above 0 (the 512-byte ring); idle under
   20% (the correlators' cost).
