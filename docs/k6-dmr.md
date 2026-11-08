# DMR on the K6: detect it, mute it, maybe decode it later

*2026-11-08.* Jeff's idea: the Baofeng UV-K6 should at least recognise DMR and mute its audio,
and perhaps decode it later. The local DMR repeater is on 440.925 MHz. Mixed-mode, it also sends
an analog CW ID, which EXPLORE keeps finding at -90 to -100 dBm.

**Answer: yes, with no hardware change, as a RAM module on today's image.** The K6 can't hear
its own receive audio: the FD6818's AF output goes straight to the speaker amplifier. But it
can see the receive IF on PB1 (GP0 code 13, the sigma-delta stream that `gp0dma` and the FT8
and softmodem modules read by DMA). In that IF's FM-demodulated frequency, DMR's 48-bit sync
words show up plainly. A correlator for them, about 3 KB of Thumb code using about 45% of the
CPU while a DMR carrier is up, finds every kind of DMR transmission in the model within 0.1 to
0.7 s. It found nothing in 30 s each of FM voice, FFSK, CW tones and noise. The `dmrdet` module
mutes the speaker (REG_47) while DMR lasts and plays FM between. A later decode can reach the
metadata (colour code, talkgroup, source ID), but not the AMBE+2 voice.

```
FD6818 low IF (16.93 kHz) -> GP0 code 13, 1-bit sigma-delta on PB1 -> DMA ch1, 5.5 MS/s, 512 B ring
  -> ones per 64 samples (85.8 kHz counts) -> rising zero crossings, interpolated
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

- **EXPLORE (firmware, after the bench run):** when it listens to a row, run the detector
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

## Decoding DMR later

- **Metadata: feasible on the M0.** The same discriminator and grid, sliced to dibits at the
  sync's timing, give the CACH (TACT, Hamming (7,4): which slot, busy or idle), the slot type
  (Golay (20,8): colour code and data type), and in a voice LC header the full link control
  (BPTC (196,96) and RS (12,9): talkgroup and source ID). Those are a few hundred bytes of code
  each, and the symbol slicer must do better than the hard decisions a detector gets away
  with. A module could print "CC1 TG 3100 from 3112345" per call. That is the natural next step
  after the bench run.
- **Voice: not planned.** DMR voice is AMBE+2. The open decoders that exist are of uncertain
  legal standing, and an AMBE+2 decoder plus the K6's PWM playback would not fit the M0
  alongside everything else anyway.
- **Other 4800-baud modes:** P25's frame sync (5575F5FF77FF) also uses only outer symbols and
  could be a fifth family at no extra cost per sample. YSF's sync uses inner symbols too: the
  same correlator works, with the gate widened.

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

## Bench plan (receive only)

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
