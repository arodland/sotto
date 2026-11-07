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
| Change REG_3E (band threshold) or REG_1F (VCO bias) to move the low end | **Worth testing.** `hfprobe` configurations. Restored after each. |
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

## Results

(none yet)
