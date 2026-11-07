"""The WSPR beacon module (c/k6/mod/wsprtx.c) on an emulated Cortex-M0: it keys up only on
440.850 once the channel is clear, sets the tone oscillator (REG_71) once a symbol through the
162 WSPR tones at 8192 / 12000 s each inside the transmit limit it asks for, identifies in MCW,
and keys down on every way out; as a beacon it keeps the 2-minute grid. Its REG_71 words,
played as audio, decode to "AG7EW CN87 10" with sim/k6digi/wsprref.py. The reference encoder
and decoder are tested here too. SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, rssi_reg, run  # noqa: E402

sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "c", "k6", "mod", "wsprtx.c")
HDR = os.path.join(ROOT, "c", "k6", "mod", "wsprtx_msg.h")
WPH = 10.32444
TEXT = "AG7EW CN87 10"
TX_MS = 300 + 110592 + 400 + 52 * 60 + 3000


def test_reference_round_trip_and_sensitivity():
    from sim.k6digi import wsprref as w
    t = w.encode(TEXT)
    assert len(t) == 162 and set(t) <= {0, 1, 2, 3}
    assert list(t % 2) == list(w.SYNC)                     # the sync vector rides bit 0
    assert w.unpack(w.pack("AG7EW", "CN87", 10)) == TEXT
    assert w.unpack(w.pack("K1ABC", "FN42", 37)) == "K1ABC FN42 37"
    fs, rng = 12000, np.random.default_rng(3)
    n = int(round(fs * w.SYM))
    f = np.repeat(1497.0 + w.DF * t.astype(float), n)
    f += 12.0 * (np.arange(len(f)) / len(f) - 0.5)           # 12 Hz of drift over the frame
    x = np.concatenate([np.zeros(fs), np.cos(2 * np.pi * np.cumsum(f) / fs), np.zeros(2 * fs)])
    noise = 0.125 / 10 ** (-25 / 10) / 2500 * fs / 2         # -25 dB in 2500 Hz
    y = 0.5 * x + rng.standard_normal(len(x)) * np.sqrt(noise)
    got = w.decode_audio(y, fs, 1500.0)
    assert got and got[0][0] == TEXT, got
    assert abs(got[0][1] - 1497.0) < 0.8 and abs(got[0][3] - 12.0) <= 1.5


def test_reference_follows_wander():
    """the bench's IC-7100 -> K6 receive (2026-11-07) wandered about +-1.8 Hz within the frame,
    more than a tone spacing: the linear drift search misses it, the tracker decodes it"""
    from sim.k6digi import wsprref as w
    rate, rng = 375 / 8, np.random.default_rng(15)
    t = w.encode(TEXT)
    f = np.repeat(-3.0 + w.DF * t.astype(float), 32)
    k = 375                                                  # 8 s: a slow random wander
    wn = np.convolve(np.convolve(rng.standard_normal(len(f) + 2 * k), np.ones(k) / k, "same"),
                     np.ones(k) / k, "same")[k:-k]
    f = f + 1.8 * wn / np.max(np.abs(wn))
    pad = np.zeros(int(2 * rate))
    z = np.concatenate([pad, np.exp(2j * np.pi * np.cumsum(f) / rate), pad])
    sig2 = 10 ** (20 / 10) * rate / 2500                     # -20 dB in 2500 Hz
    z = z + np.sqrt(sig2 / 2) * (rng.standard_normal(len(z)) + 1j * rng.standard_normal(len(z)))
    assert not w.decode_linear(z, rate)
    got = w.decode_tracked(z, rate)
    assert got and got[0][0] == TEXT, got
    assert abs(got[0][2] - 2.0) < 0.2 and abs(got[0][1] + 3.0) < 1.0, got


def test_header_is_the_encoders():
    from tools import k6_ft8
    assert open(HDR).read() == k6_ft8.wsprhdr(TEXT)


def frames(w):
    """each frame's REG_71 writes [(word, us)] between tone-1 on and off, and the off time"""
    out, cur = [], None
    for r, v, us in w:
        if r == 0x70 and v & 0x8000 and cur is None:
            cur = []
        elif r == 0x70 and v == 0 and cur is not None:
            if len(cur) > 100:
                out.append((cur, us))
            cur = None
        elif r == 0x71 and cur is not None:
            cur.append((v, us))
    return out


def audio(steps, t_end, fs=12000):
    t0 = steps[0][1]
    n = int((t_end - t0) / 1e6 * fs)
    f = np.zeros(n)
    for i, (word, us) in enumerate(steps):
        a = int((us - t0) / 1e6 * fs)
        b = int((steps[i + 1][1] - t0) / 1e6 * fs) if i + 1 < len(steps) else n
        f[a:b] = word / WPH
    x = 0.3 * np.cos(2 * np.pi * np.cumsum(f) / fs)
    z = np.zeros(2 * fs)
    return np.concatenate([z, x, z]) + 0.01 * np.random.default_rng(1).standard_normal(n + 4 * fs), fs


@need_clang
def test_builds_level_3_small():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 3 and entry == 0
    assert 24 + len(code) <= 3072, len(code)


@need_clang
def test_sends_one_frame_that_decodes():
    from sim.k6digi import wsprref
    tx = {}
    ret, said, regs, _ = run(START, src=SRC, level=3, tx=tx)
    log, w = tx["log"], tx["w"]
    assert said[0] == "wspr AG7EW CN87 10 f=1500 g=60 n=1 rx=440850000", said
    assert ret == 1, (ret, said)
    assert said[-1] == "wspr sent 1 sym=162 id=52", said
    assert [n for n, *_ in log].count("tx_on") == 1 and log[0][1] == TX_MS
    assert log[-1][0] == "tx_off" and (log[-1][2] - log[0][2]) / 1000 < TX_MS
    assert {r for r, *_ in w} <= {0x70, 0x71} and regs[0x70] == 0 and regs[0x71] == 0
    (steps, t_off), = frames(w)
    assert len(steps) == 162
    us = np.array([u for _, u in steps])
    assert np.all(np.abs(np.diff(us) - 8192 / 12 * 1000) < 1500)       # 682.67 ms a symbol
    assert abs((t_off - us[0]) - 110_592_000) < 3000
    words = np.array([v for v, _ in steps]) / WPH
    assert np.allclose(words, 1500 + wsprref.DF * wsprref.encode(TEXT), atol=0.1)
    x, fs = audio(steps, t_off)
    got = wsprref.decode_audio(x, fs, 1500.0)
    assert got and got[0][0] == TEXT, got
    assert abs(got[0][1] - 1500.0) < 0.5 and abs(got[0][3]) <= 1


@need_clang
def test_beacon_keeps_the_two_minute_grid(tmp_path):
    src = tmp_path / "wsprbcn2.c"
    src.write_text('#define WSPR_REPEAT 2\n#define WSPR_EVERY 2\n#include "%s"\n' % SRC)
    tx = {}
    ret, said, regs, _ = run(START, src=str(src), level=3, tx=tx)
    assert ret == 2, said
    ons = [us for n, _, us in tx["log"] if n == "tx_on"]
    assert len(ons) == 2 and abs((ons[1] - ons[0]) - 240_000_000) < 30_000, ons
    assert any(s.startswith("wspr next ") for s in said)


@need_clang
def test_refuses_and_gives_up():
    tx = {}
    ret, *_ = run(START, src=SRC, level=3, tx=tx, rx_hz=446_000_000)
    assert ret == -2 and "log" not in tx
    ret, *_ = run({**START, 0x67: lambda us: rssi_reg(-40)}, src=SRC, level=3, tx=tx)
    assert ret == -7 and "log" not in tx
    ret, *_ = run(START, src=SRC, level=2)
    assert ret == -1


@need_clang
def test_keys_down_on_the_old_20_s_cap_or_a_key():
    tx = {"dead_us": 22_000_000}                    # an image with the old 20 s cap
    ret, said, regs, _ = run(START, src=SRC, level=3, tx=tx)
    assert ret == -5 and tx["log"][-1][0] == "tx_off" and regs[0x70] == 0
    tx = {}
    ret, said, regs, _ = run(START, src=SRC, level=3, tx=tx, keys=((30_000_000, 30_600_000),))
    assert ret == -4 and tx["log"][-1][0] == "tx_off" and regs[0x70] == 0


@need_clang
@pytest.mark.parametrize("mark", [True, False])
def test_beacon_starts_from_the_sync_mark(tmp_path, mark):
    """wsprbcn's sync: a carrier from 5.0 s to 6.2 s, then quiet; the first frame's tones start
    10 s after it dropped (16.2 s), the next one 10 minutes on. With no mark in 150 s it says so
    and starts at once"""
    src = tmp_path / "wsprsync.c"
    src.write_text('#define WSPR_REPEAT 2\n#define WSPR_SYNC_MS 10000\n#include "%s"\n' % SRC)

    def rssi(us):
        return rssi_reg(-40 if mark and 5_000_000 <= us < 6_200_000 else -120)
    tx = {}
    ret, said, regs, _ = run({**START, 0x67: rssi}, src=str(src), level=3, tx=tx)
    assert ret == 2, said
    assert said[1] == "wspr sync: waiting for the mark", said
    (s0, _), (s1, _) = frames(tx["w"])
    if mark:
        assert abs(s0[0][1] - 16_200_000) < 20_000, s0[0]
        assert said[2] == "wspr next 10 s", said
    else:
        assert said[2] == "wspr no sync mark: starting now", said
        assert 150_000_000 < s0[0][1] < 155_000_000, s0[0]
    assert abs((s1[0][1] - s0[0][1]) - 600_000_000) < 20_000


@need_clang
def test_beacon_screen_on_a_level_6_image(tmp_path):
    """on an image with the screen calls: the sync screen with its countdown, then the slot's
    countdown, the TX bar filling over the frame, the ID, and the frames sent. On a level 3
    image (every other test here) the module never calls them: run() fails any screen call"""
    src = tmp_path / "wsprui.c"
    src.write_text('#define WSPR_REPEAT 2\n#define WSPR_SYNC_MS 10000\n#include "%s"\n' % SRC)

    def rssi(us):
        return rssi_reg(-40 if 5_000_000 <= us < 6_200_000 else -120)
    tx, lcd = {}, {}
    ret, said, regs, _ = run({**START, 0x67: rssi}, src=str(src), level=6, tx=tx, lcd=lcd)
    assert ret == 2, said
    fr = lcd["frames"]
    assert all(f["text"][0] == "WSPR BEACON  440.850" and f["text"][1] == "AG7EW CN87 10 dBm"
               and f["text"][5] == "hold a key to stop" for f in fr)
    sync = [f for f in fr if f["text"][2] == "waiting for sync"]
    assert sync and sync[0]["text"][3] == "PC: k6_wspr.py sync"
    assert sync[0]["text"][4] in ("else start in 2:30", "else start in 2:29"), sync[0]["text"]
    assert all(f["us"] < 6_200_000 for f in sync)             # none while the mark is up
    wait = [f for f in fr if f["text"][2].startswith("next slot in ")]
    assert wait[0]["text"][2] in ("next slot in 0:10", "next slot in 0:09"), wait[0]["text"]
    assert wait[0]["text"][4] == "on the UTC grid" and wait[0]["text"][3] == "sent 0 of 2"
    txf = [f for f in fr if f["text"][2] == "TX"]
    bars = [f["bar"][2][0] for f in txf]
    one = bars[:bars.index(0, 1)]                              # the first frame's
    assert one == sorted(one) and one[-1] >= 160 and txf[0]["bar"][2][1] == 162, bars
    assert len(txf) >= 2 * 40                                  # every 4 symbols, both frames
    ids = [f for f in fr if f["text"][2] == "ID AG7EW"]
    assert len(ids) == 2 and all(f["bar"][2][1] == 0 for f in ids)
    assert any(f["text"][3] == "sent 1 of 2" for f in wait)
    (s0, _), (s1, _) = frames(tx["w"])
    assert abs(s0[0][1] - 16_200_000) < 40_000, s0[0]          # the screen doesn't move the slot
    assert abs((s1[0][1] - s0[0][1]) - 600_000_000) < 40_000
