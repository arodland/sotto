"""The band surfing module (c/k6/mod/surf.c) on an emulated Cortex-M0, against the modelled
FD6818 receiver of test_k6_rxmap (RSSI restarts at -173 dBm on every tune and ramps as Phase 0
measured) and the real keypad matrix (test_k6_i2s run(..., keypad=...)): the bargraph and its
caret, peak jumps with the side keys, UP/DOWN with panning, band and zoom keys, monitor, the
squelch's open and hang, and EXIT putting the audio path back. SPDX-License-Identifier: MIT"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, run  # noqa: E402
from test_k6_rxmap import Receiver  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_screen  # noqa: E402

SRC = os.path.join(ROOT, "c", "k6", "mod", "surf.c")
AUTO = os.path.join(ROOT, "c", "k6", "mod", "surfauto.c")
FOREVER = 10**12


def go(signals, keys, rx_hz=440_600_000, level=7, regs=None, src=SRC):
    from modem import k6mod
    code, entry = k6mod.build(src)
    m = Receiver([(f, d, a, b) for f, d, a, b in signals])
    lcd = {}
    r = {**START, 0x67: m.rssi, 0x47: 0x6040, **(regs or {})}
    ret, said, regs, writes = run(r, code=(code, entry), tune=m.tune, level=level, lcd=lcd, keypad=keys,
                                  spare=4072 - len(code), count=3_000_000_000, rx_hz=rx_hz)
    return ret, said, regs, writes, lcd


def taps(keys, t0, gap=200_000, hold=100_000):
    """keys pressed one after another from t0 (us): [(from, to, key)]"""
    return [(t0 + i * gap, t0 + i * gap + hold, k) for i, k in enumerate(keys)]


def height(frame, i):
    x = 2 * i
    return sum(1 for r in range(6, 30) if frame["wf"][r][x >> 3] & (0x80 >> (x & 7)))


def caret(frame):
    """the caret's bin: the single pixel pair of its tip (row 3)"""
    row = frame["wf"][3]
    xs = [x for x in range(128) if row[x >> 3] & (0x80 >> (x & 7))]
    return xs[0] // 2 if xs else None


def sels(said):
    return [ln.split()[1] for ln in said if ln.startswith("sel ")]


def at(frames, us):
    return next(f for f in frames if f["us"] >= us)


@need_clang
@pytest.mark.parametrize("src", [SRC, AUTO])
def test_builds_level_6_and_fits_the_store(src):
    from modem import k6mod
    code, entry = k6mod.build(src)
    assert k6mod.source_level(src) == 6 and entry == 0
    assert len(code) <= 4072 - 200                 # the store, with room to grow (bitmap: stack)


@need_clang
def test_bench_build_surfs_by_itself_and_wraps():
    # 70cm from 440.600: peaks at 440.850 and 441.200, then pans up the band, wraps to 430.000
    sig = [(440_850_000, -80, 0, FOREVER), (441_200_000, -95, 0, FOREVER), (430_300_000, -90, 0, FOREVER)]
    ret, said, regs, writes, lcd = go(sig, [], src=AUTO)
    s = sels(said)
    assert s[:3] == ["440.6000", "440.8500", "441.2000"]
    assert "430.3000" in s and said[-1].startswith("end sw=")
    assert lcd["st"]["us"] >= 60_000_000 and regs[0x47] == 0x6040


@need_clang
def test_bargraph_peaks_side_keys_and_audio():
    sig = [(440_850_000, -70, 0, FOREVER), (441_200_000, -95, 0, FOREVER), (440_500_000, -105, 0, FOREVER)]
    keys = [(3_000_000, 3_200_000, "A"), (4_000_000, 4_200_000, "A"), (5_000_000, 5_200_000, "M"),
            (6_000_000, 6_200_000, "B"), (6_500_000, 6_700_000, "M"), (8_000_000, 8_300_000, "X")]
    ret, said, regs, writes, lcd = go(sig, keys)
    assert said[0] == "surf 70cm 440.600 25k" and said[-1].startswith("end sw=") and ret > 0
    # SIDE1 twice: the -70 at 440.850 and then the -95 at 441.200; SIDE2 back to 440.850
    assert sels(said) == ["441.2000", "440.8500"]
    assert {r for r, _ in writes} == {0x47} and regs[0x47] == 0x6040    # REG_47 put back
    spk = lcd["st"]["spk"]
    assert spk[0] == (0, 0) and spk[-1][1] == 0                          # speaker amp off again
    assert 3_000_000 < spk[1][0] < 3_200_000 and spk[1][1] == 4          # opens on the first jump
    fr = lcd["frames"]
    quiet = at(fr, 2_000_000)
    assert quiet["text"][0] == "SURF 70cm 25k"
    assert quiet["text"][1] == "440.6000 -137dBm"
    assert quiet["text"][5] == "439.800       441.375"
    # bins: 439.800 + 25 kHz each; the three signals stand out in order of level, the rest is floor
    hs = {i: height(quiet, i) for i in range(64)}
    assert hs[42] == 24 and 15 <= hs[56] < 24 and 8 <= hs[28] < hs[56]
    assert all(h <= 1 for i, h in hs.items() if i not in (28, 42, 56, 32))
    assert caret(quiet) == 32
    on = at(fr, 3_400_000)
    assert on["text"][1] == "440.8500 -70dBm RX" and caret(on) == 42
    assert at(fr, 5_100_000)["text"][1] == "441.2000 -95dBm RX"
    # the listening bar blinks: its column flips between frames
    a, b = on, fr[fr.index(on) + 1]
    assert {height(a, 42), height(b, 42)} == {24, 0}


@need_clang
def test_up_down_step_and_pan_past_the_edge():
    keys = taps("U" * 33 + "M", 1_000_000) + taps("DM", 9_000_000) + [(10_000_000, 10_300_000, "X")]
    ret, said, regs, writes, lcd = go([], keys)
    assert sels(said) == ["441.4250", "441.4000"]
    fr = lcd["frames"]
    assert at(fr, 900_000)["text"][5] == "439.800       441.375"
    assert at(fr, 8_900_000)["text"][5] == "440.200       441.775"   # panned a quarter span
    assert caret(at(fr, 8_900_000)) == 49


@need_clang
def test_band_and_zoom_keys():
    keys = taps("N", 1_000_000) + taps("1", 3_000_000) + taps("3", 5_000_000) + taps("NNNN", 7_000_000) \
        + [(9_000_000, 9_300_000, "X")]
    ret, said, regs, writes, lcd = go([], keys)
    fr = lcd["frames"]
    gmrs = at(fr, 2_000_000)
    assert gmrs["text"][0] == "SURF GMRS 12.5k"
    assert gmrs["text"][1].startswith("465.1375 ")          # mid-band, on the 12.5 kHz raster
    assert at(fr, 4_000_000)["text"][0] == "SURF GMRS 10k"
    assert at(fr, 4_000_000)["text"][1].startswith("465.1400 ")      # snapped to the 10 kHz raster
    assert at(fr, 6_000_000)["text"][0] == "SURF GMRS 12.5k"
    # four more: marine/WX, 2m, 1.25m, 70cm
    assert at(fr, 8_500_000)["text"][0] == "SURF 70cm 25k"
    assert at(fr, 8_500_000)["text"][1].startswith("440.0000 ")


@need_clang
def test_monitor_opens_the_audio_without_a_signal():
    keys = taps("*", 1_000_000) + taps("*", 3_000_000) + [(4_000_000, 4_300_000, "X")]
    ret, said, regs, writes, lcd = go([], keys)
    spk = lcd["st"]["spk"]
    assert [v for _, v in spk] == [0, 4, 0]
    assert 1_000_000 < spk[1][0] < 1_200_000 and 3_000_000 < spk[2][0] < 3_300_000
    fr = lcd["frames"]
    assert at(fr, 2_000_000)["text"][0] == "SURF 70cm 25k MON"
    assert at(fr, 3_500_000)["text"][0] == "SURF 70cm 25k"


@need_clang
def test_squelch_opens_on_a_signal_and_closes_after_the_hang():
    sig = [(440_600_000, -100, 2_000_000, 3_000_000)]
    ret, said, regs, writes, lcd = go(sig, [(6_000_000, 6_300_000, "X")])
    spk = lcd["st"]["spk"]
    assert [v for _, v in spk] == [0, 4, 0]
    assert 2_000_000 < spk[1][0] < 2_150_000                 # a check every 16 bins (~70 ms here)
    assert 4_500_000 <= spk[2][0] < 4_700_000                 # 1.5 s after it dropped


@need_clang
def test_exit_waits_for_the_key_release():
    ret, said, regs, writes, lcd = go([], [(1_000_000, 2_500_000, "X")])
    assert lcd["st"]["us"] >= 2_500_000 and said[-1].startswith("end sw=")


@need_clang
def test_refuses_an_image_without_a_screen():
    ret, said, regs, writes, lcd = go([], [], level=5)
    assert ret == -3 and said == [] and writes == []


def test_screen_renderer_lays_out_like_the_firmware():
    f = {"text": ["SURF", "1", "", "", "", ""], "bar": [(0, 0)] * 6, "wf": [bytes([0x80] + [0] * 15)] * 30}
    fb = k6_screen.render(f)
    assert all(fb[y][127] == 1 for y in range(11)) and fb[11][127] == 0   # inverted title bar
    assert fb[21][0] == 1 and fb[50][0] == 1 and fb[20][0] == 0 and fb[51][0] == 0
    assert sum(fb[y][x] for y in range(12, 20) for x in range(1, 7)) > 0  # the "1" on line 1


if __name__ == "__main__":
    # python tests/test_k6_surf.py OUT.png: the emulated screens of the side-key run, for docs
    sig = [(440_850_000, -70, 0, FOREVER), (441_200_000, -95, 0, FOREVER), (440_500_000, -105, 0, FOREVER)]
    keys = [(3_000_000, 3_200_000, "A"), (4_000_000, 4_200_000, "A"), (8_000_000, 8_300_000, "X")]
    _, _, _, _, lcd = go(sig, keys)
    fr = lcd["frames"]
    q = at(fr, 2_000_000)
    k6_screen.png([q, fr[fr.index(q) + 1], at(fr, 3_400_000), at(fr, 4_400_000)],
                  sys.argv[1] if len(sys.argv) > 1 else "surf.png",
                  captions=["sweeping, bar on 440.600", "the bar blinks", "SIDE1: peak 440.850, audio on",
                            "SIDE1: next peak 441.200"])
