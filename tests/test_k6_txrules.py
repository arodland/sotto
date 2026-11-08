"""The transmit rules, for every K6 module that keys up (any c/k6/mod/*.c that calls tx_on), on an
emulated Cortex-M0. A new transmitting module is checked here without a test of its own:

  - it transmits only on 440.850 MHz, and never while the channel is busy;
  - it asks tx_on for at most the core's cap (120 s since 2026-11-07, 20 s before) and
    finishes inside what it asked for;
  - every way out keys down (tx_off last), with the tone oscillator (REG_70 / REG_71) off;
  - it touches no chip register but the tone oscillator's while keyed;
  - it identifies as AG7EW in Morse (PA keying or REG_70 keying);
  - a refused tx_on, the core's time limit and a key press all end the transmission.

SPDX-License-Identifier: MIT"""
import glob
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, rssi_reg, run  # noqa: E402

# Modules that key up only when a person keys them (the paddles) run the same rules in their own
# test, with a modelled operator: morse.c in tests/test_k6_morse.py
INTERACTIVE = {"morse.c"}
MODS = sorted(p for p in glob.glob(os.path.join(ROOT, "c", "k6", "mod", "*.c"))
              if re.search(r"->\s*tx_on\s*\(", open(p).read()) and os.path.basename(p) not in INTERACTIVE)
IDS = [os.path.basename(p)[:-2] for p in MODS]
MORSE = {"A": ".-", "G": "--.", "7": "--...", "E": ".", "W": ".--", "S": "...", "O": "---", "T": "-",
         "K": "-.-", "N": "-.", "C": "-.-.", "Q": "--.-", "D": "-..", "I": "..", "R": ".-.", "M": "--"}
INV = {v: k for k, v in MORSE.items()}
LIMIT_MS = 120_000                 # the core's module TX cap (MOD_TX_MAX_MS in the Aura patch)


def level(src):
    from modem import k6mod
    return max(3, k6mod.source_level(src))


def morse(spans):
    """[(on, us)] -> the text after the last long carrier (FT8's frame, say), in Morse"""
    ons = [us for on, us in spans if on]
    if not ons:
        return ""
    unit = min(ons)
    text, sym = "", ""
    for on, us in spans:
        u = us / unit
        if on and u > 4.5:            # not Morse: start again after it
            text, sym = "", ""
        elif on:
            sym += "." if u < 2 else "-"
        elif u >= 2.5 and sym:
            text, sym = text + INV.get(sym, "?"), ""
            if u >= 5.5:
                text += " "
    return (text + INV.get(sym, "?") if sym else text).strip()


def spans_of(tx, t_end):
    """the keying as [(on, us)]: PA on/off when the module keys the PA, else REG_70 on/off"""
    def edges(ev):          # a repeat of the same state (a keep-alive) is no edge
        return [e for i, e in enumerate(ev) if i == 0 or e[0] != ev[i - 1][0]]
    pa = edges([(bool(a), us) for n, a, us in tx["log"] if n == "tx_pa"])
    if sum(on for on, _ in pa) > 1:
        ev = pa
    else:
        ev = edges([(bool(v), us) for r, v, us in tx.get("w", []) if r == 0x70])
    ev = ev + [(False, t_end)]
    return [(on, ev[i + 1][1] - us) for i, (on, us) in enumerate(ev[:-1]) if ev[i + 1][1] > us]


def test_found_the_transmitters():
    assert {"cwkey", "ft8tx"} <= set(IDS), IDS


@need_clang
@pytest.mark.parametrize("src", MODS, ids=IDS)
def test_keys_up_right_and_identifies(src):
    tx = {}
    ret, said, regs, _ = run(START, src=src, level=level(src), tx=tx)
    log = tx["log"]
    names = [n for n, *_ in log]
    assert names[0] == "tx_on" and names[-1] == "tx_off", log[:3]
    assert names.count("tx_on") == 1, names
    asked_ms, t_on, t_off = log[0][1], log[0][2], log[-1][2]
    assert 0 < asked_ms <= LIMIT_MS, asked_ms
    assert (t_off - t_on) / 1000 < asked_ms
    keyed = [(r, v) for r, v, us in tx.get("w", []) if t_on <= us <= t_off]
    assert {r for r, _ in keyed} <= {0x70, 0x71}, sorted({r for r, _ in keyed})
    assert regs.get(0x70, 0) == 0 and regs.get(0x71, 0) == 0       # the tone oscillator off
    assert "AG7EW" in morse(spans_of(tx, t_off)), (morse(spans_of(tx, t_off)), said[-1:])


@need_clang
@pytest.mark.parametrize("hz", [146_520_000, 446_000_000, 440_800_000])
@pytest.mark.parametrize("src", MODS, ids=IDS)
def test_only_on_440_850(src, hz):
    tx = {}
    ret, *_ = run(START, src=src, level=level(src), tx=tx, rx_hz=hz)
    assert ret < 0 and "log" not in tx


@need_clang
@pytest.mark.parametrize("src", MODS, ids=IDS)
def test_never_on_a_busy_channel(src):
    tx = {}
    ret, *_ = run({**START, 0x67: lambda us: rssi_reg(-40)}, src=src, level=level(src), tx=tx)
    assert ret < 0 and "log" not in tx


@need_clang
@pytest.mark.parametrize("src", MODS, ids=IDS)
def test_needs_level_3(src):
    tx = {}
    ret, *_ = run(START, src=src, level=2, tx=tx)
    assert ret < 0 and "log" not in tx


@need_clang
@pytest.mark.parametrize("src", MODS, ids=IDS)
def test_every_way_out_keys_down(src):
    lv = level(src)
    tx = {"on_ret": -1}
    ret, *_ = run(START, src=src, level=lv, tx=tx)
    assert ret < 0 and [n for n, *_ in tx["log"]] == ["tx_on"]
    # the core's time limit: the PA is dead from 1.5 s after the key-up
    tx = {}
    run(START, src=src, level=lv, tx=tx)
    dead = tx["log"][0][2] + 1_500_000
    tx = {"dead_us": dead}
    ret, _, regs, _ = run(START, src=src, level=lv, tx=tx)
    assert ret < 0 and tx["log"][-1][0] == "tx_off" and tx["log"][-1][2] < dead + 200_000
    assert regs.get(0x70, 0) == 0
    # a key press
    t_key = tx["log"][0][2] + 1_000_000
    tx = {}
    ret, _, regs, _ = run(START, src=src, level=lv, tx=tx, keys=((t_key, t_key + 5_000_000),))
    assert ret < 0 and tx["log"][-1][0] == "tx_off" and tx["log"][-1][2] < t_key + 200_000
    assert regs.get(0x70, 0) == 0
