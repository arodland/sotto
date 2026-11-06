"""The FT8 transmit module (c/k6/mod/ft8tx.c) on an emulated Cortex-M0: it keys up only on
440.850 once the channel is clear, steps the tone oscillator (REG_71) through the 79 FT8 tones
with GFSK transitions inside the transmit limit it asks for, identifies in MCW, and keys down on
every way out. The REG_71 words, played as audio, decode back to "CQ AG7EW CN87" with the K6's
own receiver. SPDX-License-Identifier: MIT"""
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, rssi_reg, run  # noqa: E402

sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "c", "k6", "mod", "ft8tx.c")
HDR = os.path.join(ROOT, "c", "k6", "mod", "ft8tx_msg.h")
WPH = 10.32444                     # REG_71 words per Hz
MORSE = {"A": ".-", "G": "--.", "7": "--...", "E": ".", "W": ".--"}


def ft8_writes(w):
    """the REG_71 writes of the frame: [(word, us)], from tone-1 on to tone-1 off"""
    on = [i for i, (r, v, _) in enumerate(w) if r == 0x70 and v & 0x8000]
    off = [i for i, (r, v, _) in enumerate(w) if r == 0x70 and v == 0 and i > on[0]]
    return [(v, us) for r, v, us in w[on[0]:off[0]] if r == 0x71], w[on[0]][2], w[off[0]][2]


def audio(steps, t_end, fs=12000, pad=1.5):
    """FM-demodulated audio of the tone oscillator: each word holds until the next write"""
    t0 = steps[0][1]
    n = int((t_end - t0) / 1e6 * fs)
    f = np.zeros(n)
    for i, (word, us) in enumerate(steps):
        a = int((us - t0) / 1e6 * fs)
        b = int((steps[i + 1][1] - t0) / 1e6 * fs) if i + 1 < len(steps) else n
        f[a:b] = word / WPH
    x = 0.3 * np.cos(2 * np.pi * np.cumsum(f) / fs)
    z = np.zeros(int(pad * fs))
    rng = np.random.default_rng(1)
    y = np.concatenate([z, x, z, z, z, z, z, z, z])
    return y + 0.01 * rng.standard_normal(len(y)), fs


def test_header_is_the_encoders():
    from tools import k6_ft8
    assert open(HDR).read() == k6_ft8.txhdr("CQ AG7EW CN87")


@need_clang
def test_builds_level_3_small():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 3 and entry == 0
    assert 24 + len(code) <= 2048, len(code)


@need_clang
def test_sends_the_frame_and_the_id():
    from sim.k6digi import ft8ref
    tx = {}
    ret, said, regs, writes = run(START, src=SRC, level=3, tx=tx)
    log, w = tx["log"], tx["w"]
    assert said[0] == "ft8tx CQ AG7EW CN87 f=1500 g=60 rx=440850000", said
    assert ret == 79, (ret, said)
    assert log[0][0] == "tx_on" and log[-1][0] == "tx_off"
    assert [n for n, *_ in log].count("tx_on") == 1
    assert {r for r, *_ in w} <= {0x70, 0x71}            # nothing refused, nothing else touched
    assert w[-2:] and regs[0x70] == 0 and regs[0x71] == 0
    steps, t_on, t_off = ft8_writes(w)
    assert len(steps) == 79 * 32
    # 5 ms steps, 160 ms symbols, a 12.64 s frame to within the emulator's call costs
    dt = np.diff([us for _, us in steps])
    assert abs(dt.mean() - 5000) < 5 and dt.max() < 6000
    assert abs((t_off - t_on) - 12_640_000) < 6000
    assert t_on - log[0][2] >= 300_000                   # the lead-in: carrier, no tone
    # each symbol's middle sits on its tone (1500 + 6.25 k Hz), its edges ramp (GFSK)
    _, tones = ft8ref.encode("CQ AG7EW CN87")
    mid = np.array([steps[32 * k + 16][0] for k in range(79)]) / WPH
    assert np.allclose(mid, 1500 + 6.25 * tones, atol=0.15)
    edge = steps[32 * 1][0] / WPH                         # 2.5 ms into symbol 1, from tone 3 to 1
    assert 1500 + 6.25 * 1 < edge < 1500 + 6.25 * 3
    # the limit it asked for covers the frame, the ID and a margin; it finished inside
    assert log[0][1] == 300 + 79 * 160 + 400 + 52 * 60 + 1000
    assert (log[-1][2] - log[0][2]) / 1000 < log[0][1]
    assert said[-1].startswith("ft8tx sent sym=79 ms=12640 id=52"), said[-1]
    # the frame decodes with the K6's own receiver, as the IC-7100's FM audio would carry it
    from sim.k6digi.ft8_wav import decode_audio
    x, fs = audio(steps, t_off)
    found = decode_audio(x, fs, lo=1300, hi=1800)
    assert "CQ AG7EW CN87" in found, found
    f0, dt0, snr = found["CQ AG7EW CN87"]
    assert abs(f0 - 1500) < 7


@need_clang
def test_mcw_id_spells_ag7ew():
    tx = {}
    run(START, src=SRC, level=3, tx=tx)
    w = tx["w"]
    i71 = [i for i, (r, v, _) in enumerate(w) if r == 0x71 and v == int(800 * WPH)]
    assert i71, "no 800 Hz word"
    keys = [(v != 0, us) for r, v, us in w[i71[0]:] if r == 0x70]
    spans = [(on, keys[i + 1][1] - us) for i, (on, us) in enumerate(keys[:-1])]
    text, sym = "", ""
    inv = {v: k for k, v in MORSE.items()}
    for on, us in spans:
        u = round(us / 60_000)
        assert abs(us - u * 60_000) < 2000, spans
        if on:
            sym += "." if u == 1 else "-"
        elif u >= 3:
            text, sym = text + inv[sym], ""
    assert text + (inv[sym] if sym else "") == "AG7EW"


@need_clang
def test_refuses_and_gives_up():
    tx = {}
    ret, said, *_ = run(START, src=SRC, level=3, tx=tx, rx_hz=446_000_000)
    assert ret == -2 and "log" not in tx
    ret, said, *_ = run({**START, 0x67: lambda us: rssi_reg(-40)}, src=SRC, level=3, tx=tx)
    assert ret == -7 and "log" not in tx
    ret, *_ = run(START, src=SRC, level=2)
    assert ret == -1


@need_clang
def test_keys_down_when_time_is_up_or_a_key():
    tx = {"dead_us": 6_000_000}
    ret, said, regs, _ = run(START, src=SRC, level=3, tx=tx)
    assert ret == -5 and tx["log"][-1][0] == "tx_off" and regs[0x70] == 0
    tx = {}
    ret, said, regs, _ = run(START, src=SRC, level=3, tx=tx, keys=((4_000_000, 4_200_000),))
    assert ret == -4 and tx["log"][-1][0] == "tx_off" and regs[0x70] == 0
