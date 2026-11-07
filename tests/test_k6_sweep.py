"""The K6 speaker sweep module (c/k6/mod/spksweep.c) on an emulated Cortex-M0, against a modelled
TIM1, and tools/k6_sweep.py, which must find the sweep in a recording and read back a known
response: the module's PWM values held at 16 kHz, through the schematic's RC filter, into a
44.1 kHz recording with noise and a delay. SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, run  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_sweep  # noqa: E402

SRC = os.path.join(ROOT, "c", "k6", "mod", "spksweep.c")
TIM1, RCC, GPIOA = 0x40012C00, 0x40021000, 0x48000000
AT_START = {"rcc": {0x18: 0x0000_0201}, "gpioa": {0x00: 0x2800_0000, 0x08: 0x0C00_0000, 0x24: 0x0000_0110}}


def emulate(keys=(), tim_busy=False, regs=None):
    """run the module: (ret, said, chip registers, log) with log = {"ccr4": [values written],
    "spk": [(on, index in ccr4)], "tim": TIM1 at the end, "rcc", "gpioa": theirs at the end}"""
    log = {"ccr4": [], "spk": [], "tim": {0x00: int(tim_busy)}, "updates": 0,
           "rcc": dict(AT_START["rcc"]), "gpioa": dict(AT_START["gpioa"]), "armed": False}

    def tim_r(uc, off, size, st):
        if off == 0x10 and log["tim"].get(0x00, 0) & 1:
            if not log["armed"]:          # the first read after a clear: not yet
                log["armed"] = True
                return 0
            log["updates"] += 1           # the next: an update event
            st["us"] += 62 + (log["updates"] & 1)   # 62.5 us an update, in whole us
            return 1
        return log["tim"].get(off, 0)

    def tim_w(uc, off, size, value, st):
        if off == 0x10:
            log["armed"] = False
        if off == 0x40:
            log["ccr4"].append(value)
        log["tim"][off] = value

    def reg_r(name):
        return lambda uc, off, size, st: log[name].get(off, 0)

    def reg_w(name):
        def w(uc, off, size, value, st):
            log[name][off] = value
        return w

    def gpiob(off, value, st):
        assert value == 1 << 2 and off in (0x18, 0x28), (hex(off), value)
        log["spk"].append((off == 0x18, len(log["ccr4"])))

    mmio = [(TIM1, 0x400, tim_r, tim_w), (RCC, 0x400, reg_r("rcc"), reg_w("rcc")),
            (GPIOA, 0x400, reg_r("gpioa"), reg_w("gpioa"))]
    ret, said, regs, writes = run(regs or {**START, 0x47: 0x6140}, src=SRC, mmio=mmio, keys=keys,
                                  gpiob=gpiob)
    return ret, said, regs, writes, log


@pytest.fixture(scope="module")
def swept():
    return emulate()


def played(log):
    """the values written while the amplifier was on, less the one queued before it went off"""
    (on, a), (off, b) = log["spk"]
    assert on and not off
    return np.array(log["ccr4"][a:b - 1], float)


@need_clang
def test_builds_level_2_small():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 2 and entry == 0
    assert 24 + len(code) <= 1500, len(code)


@need_clang
def test_plays_the_schedule_and_puts_everything_back(swept):
    ret, said, regs, writes, log = swept
    assert ret == 14
    assert said == ["spk sweep rate=16000 tones=14 top=2000", "spk sweep done tones=14 late=0"]
    assert writes == [(0x47, 0x6040), (0x47, 0x6140)]     # the chip's AF muted while it plays
    t = log["tim"]
    assert t[0x2C] == 2000 and t[0x30] == 2 and t[0x28] == 0 and t[0x00] == 0 and t[0x0C] == 0
    assert not t[0x44] & 0x8000 and t[0x20] == 0         # outputs off, CC4E as found
    assert log["rcc"] == AT_START["rcc"] and log["gpioa"] == AT_START["gpioa"]
    x = played(log)
    fs = k6_sweep.RATE
    assert len(x) == 16000 + 14 * 20000
    assert log["ccr4"][:481] == [1000] * 481             # 30 ms settled before the amplifier
    assert log["ccr4"][-321:] == [1000] * 321            # and 20 ms after it
    assert np.ptp(x[:int(0.99 * fs)]) == 0 and x[0] == 1000
    for j, f in enumerate(k6_sweep.HZ):
        a = int((1 + 1.25 * j + 0.1) * fs)
        seg = x[a:a + int(0.8 * fs)] - 1000
        assert 230 <= seg.max() <= 250 and -250 <= seg.min() <= -230   # peak, as sampled
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        assert abs(np.argmax(sp) * fs / len(seg) - f) < 2
        g = x[int((2.0 + 1.25 * j) * fs):int((2.25 + 1.25 * j) * fs)]
        assert np.ptp(g) == 0 and g[0] == 1000               # the gap is silent


@need_clang
def test_a_key_stops_it_in_a_silence():
    # the gap after tone 1 starts 3.25 s in (plus the 30 ms settle)
    ret, said, regs, writes, log = emulate(keys=((3_200_000, 3_400_000),))
    assert ret == -2 and said[-1] == "spk sweep done tones=2 late=0"
    assert writes[-1] == (0x47, 0x6140) and log["tim"][0x00] == 0
    assert [on for on, _ in log["spk"]] == [True, False]
    assert log["gpioa"] == AT_START["gpioa"]


@need_clang
def test_leaves_a_running_timer_alone():
    ret, said, regs, writes, log = emulate(tim_busy=True)
    assert ret == -4 and not writes and not log["ccr4"] and not log["spk"]


@need_clang
def test_reads_back_a_known_response(swept, tmp_path):
    from scipy.signal import bilinear, freqz, lfilter, resample_poly
    sf = pytest.importorskip("soundfile")
    x = (played(swept[4]) - 1000) / 1000
    y = np.repeat(x, 3)                          # 16 kHz values held at 48 kHz
    # the ladder as an analog filter, matched at 48 kHz (its poles sit at 1.5 and 8.7 kHz)
    r1, c1, r2, c2 = 4.7e3, 3.9e-9, 4.7e3, 22e-9
    b, a = bilinear([1.0], [r1 * c1 * r2 * c2, r1 * c1 + r2 * c2 + r1 * c2, 1.0], 48000)
    y = lfilter(b, a, y)
    y = resample_poly(y, 147, 160)               # 44.1 kHz
    rng = np.random.default_rng(1)
    y = np.concatenate([np.zeros(int(1.7 * 44100)), y, np.zeros(44100)])
    y = y + rng.normal(0, 1e-3, len(y))
    sf.write(tmp_path / "rec.wav", y, 44100)
    r = k6_sweep.analyse(str(tmp_path / "rec.wav"))
    assert abs(r["start_s"] - 2.7) < 0.03
    hz = np.array(k6_sweep.HZ, float)
    _, h = freqz(b, a, worN=np.append(hz, 1000.0), fs=48000)
    want = 20 * np.log10(np.abs(h[:-1]) / np.abs(h[-1]))
    for t, w in zip(r["tones"], want):
        assert abs(t["db"] - w) < 0.7, (t, w)
        assert t["snr_db"] > 30
