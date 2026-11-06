"""The CW ID module (c/k6/mod/cwkey.c) on an emulated Cortex-M0: it keys up only on 440.850,
sends AG7EW as PA on/off keying at 20 wpm inside the transmit limit it asks for, keys down on
every way out, and decodes back to its text. SPDX-License-Identifier: MIT"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, rssi_reg, run  # noqa: E402

SRC = os.path.join(ROOT, "c", "k6", "mod", "cwkey.c")
BUSY = {**START, 0x67: lambda us: rssi_reg(-40 if us < 2_500_000 else -110)}
UNIT = 60_000            # us per unit at 20 wpm
MORSE = {"A": ".-", "G": "--.", "7": "--...", "E": ".", "W": ".--"}


def keying(log):
    """The PA's on and off spans after tx_on, in us: [(on, us), ...]"""
    pa = [(a, us) for n, a, us in log if n == "tx_pa"]
    return [(a, pa[i + 1][1] - us) for i, (a, us) in enumerate(pa[:-1]) if pa[i + 1][1] > us]


def decode(spans):
    text, sym = "", ""
    for on, us in spans:
        u = round(us / UNIT)
        if on:
            sym += "." if u == 1 else "-"
        elif u >= 3 and sym:
            text += {v: k for k, v in MORSE.items()}[sym]
            sym = ""
    return text + ({v: k for k, v in MORSE.items()}[sym] if sym else "")


@need_clang
def test_builds_level_3_small():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 3 and entry == 0
    assert 24 + len(code) <= 1500, len(code)


@need_clang
def test_sends_ag7ew_and_keys_down():
    tx = {}
    ret, said, regs, writes = run(START, src=SRC, level=3, tx=tx)
    log = tx["log"]
    assert said[0] == "cw key AG7EW 20wpm rx=440850000", said
    assert log[0][0] == "tx_on" and log[1][:2] == ("tx_pa", 0)
    assert log[-2][:2] == ("tx_pa", 0) and log[-1][0] == "tx_off"
    assert [n for n, *_ in log].count("tx_on") == 1
    spans = keying(log)
    assert decode(spans) == "AG7EW"
    # every element and gap is a whole number of 60 ms units, within the emulator's call costs
    assert all(abs(us - round(us / UNIT) * UNIT) < 2000 for _, us in spans[1:]), spans
    assert spans[0] == (0, spans[0][1]) and 300_000 <= spans[0][1] < 305_000   # lead-in, PA off
    units = sum(round(us / UNIT) for _, us in spans[1:])
    assert ret == units == 52   # A 8, G 12, 7 16, E 4, W 12 (each with its 3-unit gap)
    # the limit it asked for covers the lead-in, the text and a margin, and it finished inside
    on_ms, t_on = log[0][1], log[0][2]
    assert on_ms == 300 + 52 * 60 + 1000
    assert (log[-1][2] - t_on) / 1000 < on_ms
    assert said[-1].startswith("cw sent units=52 ms=")
    assert not writes                     # no register writes: the core does the keying


@need_clang
def test_waits_for_the_sender_to_unkey():
    tx = {}
    ret, said, *_ = run(BUSY, src=SRC, level=3, tx=tx)
    assert ret == 52 and tx["log"][0][0] == "tx_on"
    assert 3_450_000 <= tx["log"][0][2] < 3_650_000, tx["log"][0]    # 1 s after it cleared


@need_clang
def test_gives_up_on_a_busy_channel():
    tx = {}
    busy = {**START, 0x67: lambda us: rssi_reg(-40)}
    ret, said, *_ = run(busy, src=SRC, level=3, tx=tx)
    assert ret == -7 and "log" not in tx


@need_clang
def test_refuses_off_the_bench_frequency():
    tx = {}
    ret, said, *_ = run(START, src=SRC, level=3, tx=tx, rx_hz=146_520_000)
    assert ret == -2 and "log" not in tx


@need_clang
def test_refuses_without_level_3():
    tx = {}
    ret, said, *_ = run(START, src=SRC, level=2, tx=tx)
    assert ret == -1 and not said and "log" not in tx


@need_clang
def test_tx_on_refused():
    tx = {"on_ret": -1}
    ret, said, *_ = run(START, src=SRC, level=3, tx=tx)
    assert ret == -3 and [n for n, *_ in tx["log"]] == ["tx_on"]


@need_clang
def test_key_press_keys_down():
    tx = {}
    ret, said, *_ = run(START, src=SRC, level=3, tx=tx, keys=((2_000_000, 9_000_000),))
    assert ret == -4
    assert tx["log"][-2][:2] == ("tx_pa", 0) and tx["log"][-1][0] == "tx_off"
    assert 2_000_000 <= tx["log"][-1][2] < 2_100_000


@need_clang
def test_time_up_stops_keying():
    tx = {"dead_us": 3_000_000}
    ret, said, *_ = run(START, src=SRC, level=3, tx=tx)
    assert ret == -5 and tx["log"][-1][0] == "tx_off"
    assert all(us < 3_100_000 for _, _, us in tx["log"])
