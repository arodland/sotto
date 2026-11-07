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
(60 us lag) and a latched one (the result on air: latched).

### Bench run

1. K6 in VOICE MSG on 440.850, serial cable in, the listener logging ttyUSB2 (read only).
2. Send the module (CI artifact `mod-xtalstep.bin`, about 2.8 KB):
   `python -m modem.k6 mod xs.wav mod-xtalstep.bin --api-level 2 --key-file ~/.config/sotto/k6-mod.key`
   and play `xs.wav` as for any module.
3. When the K6 says `xs wait carrier`, key the IC-7100 on 440.850 with no audio for about
   15 s (the run takes about 1.5 s for A and B, and 4.3 s for C).
4. The log's `xs a`, `xs b`, `xs l` or `c up/dn` lines are the result.

## Result on the air (2026-11-06 10:25 UTC)

Run hands-free on hamlet: image e46e7bd, module from CI (2756 B), 440.850 MHz, the IC-7100's
bare carrier. Log `~/sotto-work/logs/xtalstep-20261106T102217Z.txt` on hamlet.

```
xs regs 3b=5880 3c=4f88 38=af1e 39=02a0 73=4682 43=3028
xs a 0 if=16763   xs a 6 if=16764   xs a -6 if=16765   xs a 24 if=16763   xs a -24 if=16763
xs a 48 if=16764  xs a -48 if=16764  (n=1544 IF cycles each, ks=5693)
xs b 100 if=16763   xs b -100 if=16762   xs b 0 if=16763
xs l 24 if=18798 n=1731   xs l 0 if=16763
```

- **REG_3B is latched.** Writes of +-6, +-24 and +-48 left the IF within 3 Hz. The chip takes the
  crystal value only at RXON/TXON.
- **So is REG_38**: +-1 kHz with no RXON changed nothing, as the guide says.
- **After RXON the step is exact:** +24 moved the IF by +2035 Hz, 84.8 Hz a step (predicted
  84.78). A larger REG_3B lowers the LO, and the IF rises.
- The crystal register reads 26 MHz (0x4F5880). REG_38/39 = 0x02A0AF1E = 440,850,220 Hz: Aura
  folds a +220 Hz crystal correction into the frequency word. REG_3C's low byte reads 0x88.
- C (the step response) didn't run, because nothing moved live.

So REG_3B is not a per-symbol modulator. It is a fine-tuning knob (84.8 Hz steps, set before
TXON) that the guard could use to trim the K6's carrier against the IC-7100.

## CW ID from the K6 (cwkey)

Jeff asked (2026-11-06) for the K6 to send AG7EW once in true CW: the carrier keyed by the PA
with the PLL locked. `c/k6/mod/cwkey.c` (level 3, about 1 KB) does it with the level-3 calls
only: it says `cw key AG7EW 20wpm rx=...`, keys up only if the pager listens on 440.850 and the
channel has stayed below -90 dBm for 1 s (the module runs as its last chunk lands, while the
IC-7100 is still sending its own CW ID; -7 if it isn't clear within 30 s), asks
tx_on for the lead-in plus the text plus 1 s (4.42 s for AG7EW), turns the PA off for a 300 ms
lead-in, keys the text at 20 wpm with tx_pa (60 ms units, 52 for AG7EW), then tx_pa(0) and
tx_off, and says `cw sent units=52 ms=3420`. It writes no registers. A key press keys down at
once (-4), and so does the core's time limit (-5). With the PA off between elements a receiver
hears it as CW in CW or SSB mode; in FM it hears only the squelch opening and closing.

Tests: `tests/test_k6_cwkey.py` decodes the PA keying back to AG7EW on the emulated M0 and
checks the wait for a clear channel, the refusals (wrong frequency, no level 3, tx_on refused,
a busy channel) and both ways out. It needs a
K6 image with module API level 3 (PR #71's `aura-pager-only` or `aura-pager-lr`; the lean images
refuse tx_on).

First on-air try (2026-11-06 15:13, image 234298c): the upload and the wait for a clear channel
worked, then `K6 MOD tx refused hz=440850000`. Aura's enter_tx checks the radio's band lock
(FLOCK), whose default "CE HAM" allows TX only on 144-146 and 430-440 MHz, and the pager image
has no menu to change it. Jeff chose to give the module transmitter its own lock: tx_on now
allows the US amateur 2 m and 70 cm bands (FCC HAM, 144-148 and 420-450 MHz) for its key-up
only, and puts the radio's FLOCK back straight after.

Second try (15:46 UTC, image e57af49, the same 1096 B module): **the K6 sent AG7EW.** The upload
took one send. The module keyed up 1.1 s after the IC-7100 unkeyed, and returned 52:

```
15:46:42.635 K6 MOD say cw key AG7EW 20wpm rx=440850000
15:46:54.150 K6 MOD tx on hz=440850000 max=4420 ms
15:46:57.153 K6 MOD tx off
15:46:57.154 K6 MOD say cw sent units=52 ms=3432
```

The IC-7100 recorded it in USB 2400 as a ~160 Hz beat note for 3420 ms. It decodes as AG7EW:
dots 50-70 ms, dashes 170-180 ms, gaps 60-70 and 170-180 ms, after a 290 ms lead-in. On this
bench (radios a metre apart) the carrier still comes through with the PA off, about 40 dB down,
so the IC-7100's AGC shows the keying as edges. That is leakage past the PA. At range the PA-off
spans would be silent.

## Next

REG_3B turned out to be latched, so MCU-written FSK needs another source. Module API
level 3 (a guarded transmitter: tx_on/tx_off/tx_pa on the pager's frequency, low power, at most
20 s) is in the firmware patch, but not yet on the radio. What's left to try, none tested:

- the tone oscillator (REG_70/71), which is continuous-phase audio FSK on the FM carrier. It is
  not direct FSK, but the frequency word can change per symbol;
- the chip's own FSK modem (REG_58/59), which already sends our packets as 2-FSK and FFSK;
- a latched REG_3B step per TXON, if a whole packet can stay on one frequency (not FSK).
