# Can the K6 hear HF?

Jeff asked (2026-11-07) whether there is really no way for the Baofeng K6 to listen below 18 MHz.
The rules: no hardware changes, receive only, and a RAM module before any reflash.

## What we know

- **18 MHz was never measured.** The coverage run (docs/k6-rxmap.md, 2026-11-07) *started* at 18 MHz,
  the FD6818/BK4819 datasheet's lower limit. "18 to 630 MHz" means that nothing was heard above 630.
  Nobody had tuned below 18.
- **Nothing in the firmware stops a lower frequency.** Aura's `set_frequency_hz` writes the frequency in
  10 Hz units to REG_38/39 with no clamp. `retune_rx` picks the VHF front-end path (MCU PA14) for
  anything under 300 MHz. The module API's `tune` is that call. So `rxmap` can scan 1-18 MHz as it
  stands, with no new code and no flash.
- **18-30 MHz is already HF.** The radio covers it in principle: 17 m, 15 m, 12 m, 10 m, CB, WWV 20 and 25,
  and the 13 m and 11 m broadcast bands. The 01:17 UTC scan saw nothing there. It ran at night, on the
  stock whip, in 25 kHz steps.

## Where the low end comes from

The chip makes its LO by dividing a VCO. egzumer's driver notes that REG_3E (0xA037 here) is the band
selection threshold, `VCO max / 96 / 640`. That gives a VCO top of about 2.52 GHz, and 2.52 GHz / 4 = 630 MHz,
which is exactly where our run found the low band ending. So the bottom of the range is the VCO's lowest
frequency over the largest divider. The datasheet says 18 MHz. UV-K5 firmware forks on the same chip family
claim 14-15 MHz, but nobody has published a measurement. If that's true, it reaches **20 m (14.000-14.350,
FT8 at 14.074)**, WWV 15, CHU 14.670 and the 19 m and 16 m broadcast bands. It doesn't reach 40 m or 80 m.

## Ideas, ranked

| idea | verdict |
|---|---|
| Tune below 18 MHz and see where the synthesizer really stops | **Worth testing now.** `rxmap` segments `hf` (8-30 MHz) and `hflo` (1-8 MHz). |
| Change REG_3E (band threshold) or REG_1F (VCO bias) to move the low end | Tested REG_3E: no change. Run 5 shows the divider ends at /96 and the VCO's coarse tune at ~1.5 GHz. |
| Choose the front-end path at HF: UHF path, no path, or both (MCU PA13/PA14) | **Worth testing.** Out of band, the VHF path may not be the leakiest. `hfprobe` paths. |
| Demodulate 20 m SSB and FT8 in software from the GP0 IF tap | **Works if the signal gets in.** ft8wf already does this on 70 cm. Retarget its tune. |
| Mixer harmonics or images to reach below the LO | **No.** A mixer responds at n x LO +- IF, all at or above the LO. The image is only 17 kHz away. |
| Mix with the radio's own 26 MHz crystal (signal at f shows at 26 +- f) | **No in practice.** The product sits roughly 60 dB under the signal (-70 dBm leakage into an on-chip IIP2). |
| Sample the antenna directly with the MCU (PB1 is ADC_IN9) | **No.** PB1 carries the chip's GP0 IF output, after the mixer. No antenna signal reaches the MCU. |
| A longer wire on the SMA connector | Not a board change, but it's Jeff's call. At 15-28 MHz the stock 2 m / 70 cm whip is about 40 dB down on a dipole. |

## How to tell that it's really tuned

A probe frequency only counts if a signal of known frequency turns up *there*, and not 30 kHz away. Free
markers on the bench:

- The radio's own clocks: the FD6818's 26 MHz crystal and its 13 MHz half, and the 24 MHz family
  (12, 6 MHz). The first run found their harmonics everywhere, so their low harmonics should show too.
- The time stations: WWV (Fort Collins) on 2.5, 5, 10, 15, 20 and 25 MHz, and CHU (Ottawa) on 3.330, 7.850
  and 14.670 MHz. They're strongest around dawn and dusk.

If the synthesizer stops at F, every probe below F reads the same as the floor, or shows F's signal at
the wrong place. A hung PLL shows as a flat line in `hflo`.

## Running it (hamlet)

```sh
cd ~/sotto
python3 tools/k6_rxmap.py map hf hflo --bin mod-rxmap.bin --key-file ~/.config/sotto/k6-mod.key   # ~30 s + ~10 s
python3 tools/k6_hf.py plan
python3 tools/k6_hf.py probe --bin mod-hfprobe.bin --key-file ~/.config/sotto/k6-mod.key          # ~10 s
```

Both are receive only. The K6 transmits nothing except its usual report on 440.850. `hfprobe` writes the
MCU's PA13/PA14 (the front-end path pins, never the TX path) and at most one FD6818 register per
configuration. It puts both back, and the core refuses REG_30/33/36/37 as always.

`c/k6/mod/hfprobe.c` is a level 2 module (1.5 KB at -Oz). `tools/k6_hf.py` handles plan, probe, decode
and patch, and `tests/test_k6_hf.py` runs the module on the emulated M0 against a receiver whose
synthesizer stops at a set frequency.

## A register that might go lower

The FD6818 guide (chip 0x6818) doesn't leave the divider to the chip. REG_1AH takes a band code from its
table: `Band_740M_1120M` 0x1f80 (divide by 2), 2f50 (/4), 9f30 (/6), 3f48 (/8), af28 (/12), 4f44 (/16),
bf24 (/24), 5f42 (/32), cf22 (/48), 6f41 (/64), down to `Band_16M_23M` 0xdf21 (/96). That gives a VCO of
about 1.48-2.24 GHz, so divide by 96 bottoms out at 15.4 MHz. That fits the ~16.5 MHz edge run 1 found.
The codes come in two series: high nibble 1-6 with low byte 80, 50, 48, 44, 42, 41 (/2^n), and 9-d with
30, 28, 24, 22, 21 (/3 x 2^n). If the silicon carries the next step of each, **0x7f40 would be /128
(11.6-17.5 MHz, all of 20 m) and 0xef20 /192 (7.7-11.7 MHz, 30 m and WWV 10)**. The guide doesn't list
them, and nothing says the K6's 0x4819 has a hand band select at all. But its floor sits where the guide's
last band ends, which suggests the same divider chain.

The guide also gives **REG_0CH bit 9 = PLL lock**. REG_0C is one of the addresses the regmap found to be
the same on both chips, so the K6 may report lock directly. That would map the synthesizer's real range
with no test signal at all.

`c/k6/mod/bandreg.c` (level 2, 860 B, no register writes) is the first step. At each frequency of a list,
it tunes, dumps registers 0x00-0x7F twice (skipping the FSK FIFO), and prints REG_0C and every register
that is stable and differs from the 440.850 baseline. `python3 tools/k6_hf.py bandreg --bin mod-bandreg.bin
--key-file KEY` walks from 440.85 down to 10 MHz. If a register shows the band code, the next step is a
module that writes the /128 code at 14 MHz and checks lock and the floor. Receive only.

**Run 5 answered this (below): REG_1A does show the band code, REG_0C bit 9 is lock, and the code's bits leave
no room for /128.**

## Results

### Run 1, 2026-11-07 15:02-15:06 UTC (receive only, image 4709d7e, stock whip)

`rxmap hf` and `hflo`, then `hfprobe` with the default probes and configurations. Every upload went through on
the first send. The telemetry keeps only the newest 684 bytes, so the per-bin lines below 27 MHz (hf), below
3.5 MHz (hflo) and the hfprobe p lines of configurations 0-3 came back only as 2 dB summary rows.

- **Below about 16.5 MHz the floor is dead flat**, -110 dBm (+-2 dB quantisation) at every bin from 1 to 16.5
  MHz. Above 16.75 MHz it moves between -100 and -116 dBm, and that texture is outside noise getting in. The
  sharp edge between the two looks like the synthesizer's real bottom (VCO_min / N_max), about 1.5 MHz under
  the 18 MHz spec.
- Peaks 10 dB or more over the floor: 16.015 (-91, right at the edge), 20.590 (-90), 21.315 (-98, 15 m) and
  18.500 MHz (-98). None is on a clock grid. They could be real signals or harmonic-mixing images of FM
  broadcast. Not identified.
- hfprobe heard no marker 10 dB over its floor below 18 MHz in any configuration. WWV and CHU didn't show at
  any frequency, and neither did the 12 and 13 MHz clock markers.
- **Front-end path off (PA13 and PA14 both low): the floor drops 38 dB to -148**, and the FD6818's own 26 MHz
  crystal shows +12 dB at 26.000. So the -110 floor comes in through the LNA path. A better antenna would help
  above the edge, and "no path" is the clean way to look for internal markers. UHF path -124, both paths
  -114. REG_3E at 0x8000 or 0xC000 changed nothing visible.

### Runs 2 and 3: PSK31 from the IC-7100 into a dummy load

Jeff OK'd it: RF power 0.0, PSK31 "AG7EW TEST" over and over. It also set a project rule on 2026-11-07:
every test transmission carries ident, and continuous carriers go out as PSK31, so `cal` sends PSK31 on
2 m / 70 cm too.

```sh
python3 tools/k6_rxmap.py cal --hf --bin mod-rxmap.bin --key-file ~/.config/sotto/k6-mod.key
```

USB dial 18.100 is the positive control (above the spec) and 14.070 (20 m) is the question. The K6 listens
1 kHz up (18.101, 14.071), where the PSK31 tone puts the signal. `modem/psk31.py` is the encoder: G3PLX
varicode, matching fldigi's table, with a raised-cosine envelope.

- Run 2 (15:18-15:20 UTC): the bursts went out, but the tool missed the K6's report. With no carrier seen,
  the module reports at max_s, and the tool listened for only 60 s. Fixed: it now waits until max_s + 30 s,
  and `--hf` leaves 440.850 out of the K6's points.
- **Run 3 (15:25-15:27 UTC): the K6 heard neither burst**: `C 18.101:-105 14.071:-110`, `K ev=0`, returned -6.
  Because 18.101 (inside the spec) didn't show either, this says nothing about 14 MHz. It only shows that
  2 W into a dummy load doesn't reach the K6's front end at HF. A real test needs a radiating source near the
  K6, such as a short wire on the IC-7100's HF port at power 0 (on air, with ident: Jeff's call).
- Run 4 (15:29 UTC): the same at RF power 0.05. `C 18.101:-105 14.071:-110`: still nothing at either
  frequency. Jeff called it at 15:33: the hardware probably isn't leaking much, so the bench went back to WSPR
  and the question went back to the registers.

### Run 5, 2026-11-07 17:22-17:25 UTC: bandreg (receive only, no register writes, image 7d5f3ed)

Three passes, so that each fits the 684-byte telemetry; each pass's first frequency is its baseline. All three
uploads went through on the first send and all three tables came back whole.

```
     MHz lock    0c    1a    1b    38    39      VCO if N = 96 / 64 / 32 / 16
 145.000  yes 0201  5844  438a  40a7  00dd      2.320 GHz (/16)
  60.000  yes 0281  5842  5225  8d83  005b      1.920 GHz (/32)
  28.000  yes 0281  5841  61a2  b981  002a      1.792 GHz (/64)
  23.500  yes 0201  5821  d354  dbb1  0023      2.256 GHz (/96)
  20.000  yes 0281  5821  d224  8481  001e      1.920 GHz
  18.000  yes 0281  5821  d149  7740  001b      1.728 GHz
  17.000  yes 0281  5821  d096  f0a0  0019      1.632 GHz
  16.500  yes 0281  5821  d045  2d50  0019      1.584 GHz
  16.000  yes 0281  5821  d000  6a00  0018      1.536 GHz
  15.500   NO 0081  5821  d000  a6b0  0017      1.488 GHz
  15.000   NO 0081  5821  d000  e360  0016
  14.000   NO 0081  5821  d000  5cc0  0015
  12.000   NO 0081  5821  d000  4f80  0012
  10.000   NO 0081  5821  d000  4240  000f
```

(Pass C's baseline was 16.5 MHz, so REG_1A = 5821 below it is "unchanged", not printed. RSSI and AGC
registers 0x61-0x67 and 0x7E also moved; they follow the signal, not the tuning.)

- **REG_0C bit 9 is the PLL lock bit on this chip too.** It is set at every frequency down to 16.000 MHz and
  clear from 15.500 MHz down. So the K6's real bottom is between 15.5 and 16.0 MHz, a little under the 16.5 MHz
  edge that run 1 read off the noise floor. (Bit 7 comes and goes with the signal.)
- **REG_1A's low byte is the guide's band code, and the chip picks it itself**: 0x44 (/16) at 145, 0x42 (/32) at
  60, 0x41 (/64) at 28 and 0x21 (/96) from 23.5 down. The high byte reads 0x58, not the guide's 0x?f.
- **The bits explain the table, and they say /96 is the end.** Bits 7-5 pick a prescaler, one-hot: 0x80 /2
  (alone), 0x40 /4, 0x20 /6. Bits 4-0 pick a power of two after it, one-hot: 0x10 x1, 0x08 x2, 0x04 x4,
  0x02 x8, 0x01 x16. Every code in the guide fits (0x50 = 4x1, 0x48 = 4x2 ... 0x21 = 6x16). There is no bit
  left for x32, so the guessed 0x7f40 (/128) and 0xef20 (/192) don't exist: 0x40 alone is "/4 and no output".
- **REG_1B's low 10 bits are the VCO's coarse tune code, and they hit zero at 16 MHz.** 0x354 at 2.256 GHz,
  0x224 at 1.920 GHz (0x225 at 60 MHz, the same VCO frequency on another divider), down to 0x045 at 1.584 and
  0x000 from 1.536 GHz. The PLL holds on at code 0 for one more step, then loses lock. The VCO's bottom is
  about 1.5 GHz, the guide's figure.
- REG_38/39 is the tuned frequency in 10 Hz units plus about 0.5 ppm (145 MHz reads 145.000070), presumably the
  crystal correction.

**Verdict.** The divider chain stops at 6 x 16 = 96 and the VCO stops at about 1.5 GHz, so the K6 tunes down to
somewhere between 15.5 and 16.0 MHz and no further: the top of the 19 m broadcast band (15.1-15.8 MHz) at
best, not WWV 15 and not 20 m. 20 m would need
the VCO at 1.35 GHz, 12% below where its coarse tune runs out. Two long shots remain, both receive only and
restored after: (1) write REG_1A with two post-divider bits set (0x23, 0x2x) at 14 MHz, in case the taps chain;
(2) at 15.5 MHz, step the VCO bias/current registers and watch whether lock comes back, which would show how
far the VCO can be pulled. Neither is likely to reach 14 MHz.
