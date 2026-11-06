"""K6 IF matched-filter receiver (c/k6/ifrx.c) and its live module (c/k6/mod/iflive.c).

The receiver is built for this machine and fed GP0 counts from the study's model (sim/k6if:
the IC-7100's FM, the FD6818's filter and AGC, the code 13 sigma-delta at the K6's DMA rate):
its bit error rate at the C/N0s where the float study put 1%, through a carrier 164 Hz off
(the IF offset the FT8 thread measured), no lock on a bare carrier, and lost counts (the ring
overrunning) passed over without losing the symbol clock. Then the live module runs on an
emulated Cortex-M0 against the modelled DMA stream (tests/k6_live_emu.py): it must lock on
each step's lead, decode the PN9 with no counts lost, and stay inside the K6's stack and
store. Needs a C compiler; the module tests also clang, ld.lld and unicorn.
SPDX-License-Identifier: MIT"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modem import k6mod  # noqa: E402
from sim.k6if import mcu, modes as md  # noqa: E402

need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")
P = ctypes.c_void_p
I = ctypes.c_int


@pytest.fixture(scope="module")
def lib():
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    d = tempfile.mkdtemp()
    so = os.path.join(d, "libifrx.so")
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                    os.path.join(K6, "ifrx_host.c"), "-o", so], check=True)
    L = ctypes.CDLL(so)
    L.ifrx_run.argtypes = [P, I, I, ctypes.c_uint, I, I, I, P, P, P, P, I, P, P]
    return L


def mode_of(name):
    tones, baud, lv = md.MODES[name]
    return md.Mode(tones, baud, lv)


def run(L, c, m, ks=5490, start=0, pause=0, every=0, maxsym=20000):
    """-> symbols, gap flags, next symbol starts (counts), info"""
    c = np.ascontiguousarray(np.clip(c, -127, 127).astype(np.int8))
    sym, met = np.zeros(maxsym, np.uint8), np.zeros(maxsym * 4, np.uint32)
    afc, gap = np.zeros(maxsym, np.int32), np.zeros(maxsym, np.uint8)
    info, taus = np.zeros(10, np.int32), np.zeros(maxsym, np.uint32)
    n = L.ifrx_run(c.ctypes.data, len(c), 1 if m.M == 4 else 0, ks, start, pause, every,
                   sym.ctypes.data, met.ctypes.data, afc.ctypes.data, gap.ctypes.data, maxsym,
                   info.ctypes.data, taus.ctypes.data)
    return sym[:n], gap[:n], taus[:n] / 256.0, info


def ser_by_time(lk, sym, taus):
    """symbol errors with each decision put at its true symbol by its time (taus: the next
    symbol's start), so symbols passed over don't shift the rest"""
    pc = mcu.FC / lk.m.baud
    k0 = np.round(((taus - pc) / mcu.FC - lk.t0) * lk.m.baud).astype(int) - lk.lead
    best = None
    for d in range(-3, 4):
        k = k0 + d
        ok = (k >= 0) & (k < len(lk.syms))
        e = np.mean(sym[ok] != lk.syms[k[ok]]) if ok.sum() > 100 else 1.0
        if best is None or e < best[0]:
            best = (e, ok.sum())
    return best


def ber(lk, sym):
    """bit error rate of the decisions against the sent data, by the best alignment"""
    b = lk.m.bits_of
    best = 1.0
    for d in range(0, len(sym) - 500):
        n = min(len(lk.syms), len(sym) - d)
        if np.mean(sym[d:d + n] != lk.syms[:n]) < 0.3:
            best = np.mean(b[sym[d:d + n]].ravel() != b[lk.syms[:n]].ravel())
            break
    return best


@pytest.mark.parametrize("name,cn0,off,limit", [("ffsk1200", 45, 0, 0.006), ("ffsk1200", 45, -164, 0.006),
                                                ("4fsk1200", 47, 0, 0.008), ("4fsk1200", 47, -164, 0.008),
                                                ("4fsk1200", 47, 164, 0.008)])
def test_ber_on_the_model(lib, name, cn0, off, limit):
    """1% BER in the study: FFSK 42.7 dB-Hz, 4FSK 45.1; here 2 dB over that, through a 0.3 s
    bare carrier ahead of a 1.5 s lead"""
    m = mode_of(name)
    lk = mcu.Link(m, 2400, lead=1800, f_off=off, pre=0.3, seed=3)
    sym, gap, taus, info = run(lib, lk.counts(cn0, seed=5), m, start=int(0.02 * mcu.FC))
    assert info[5] == 2 and info[3] == 0, info          # tracking, no acquisition thrown out
    assert ber(lk, sym) < limit


@pytest.mark.parametrize("name", ["ffsk1200", "4fsk1200"])
def test_no_lock_on_a_bare_carrier(lib, name):
    m = mode_of(name)
    lk = mcu.Link(m, 200, lead=2, pre=3.0, seed=4)
    for cn0 in (44, 55):
        c = lk.counts(cn0, seed=cn0)[:int(2.9 * mcu.FC)]
        sym, gap, taus, info = run(lib, c, m)
        assert len(sym) == 0 and info[2] >= 6, info     # sweeps, none taken


@pytest.mark.parametrize("name", ["ffsk1200", "4fsk1200"])
def test_lost_counts_keep_the_clock(lib, name):
    """430 counts (5 ms, a report line) lost every 20000: the symbols they cover are passed
    over, the rest decode at their true places"""
    m = mode_of(name)
    lk = mcu.Link(m, 3000, lead=1800, pre=0.2, seed=6)
    c = lk.counts(52, seed=8)
    sym, gap, taus, info = run(lib, c, m, start=int(0.02 * mcu.FC), pause=430, every=20000)
    assert info[4] > 20 and gap.sum() >= 4, info        # symbols passed over, flagged
    ser, n = ser_by_time(lk, sym, taus)
    assert n > 2000 and ser < 0.01, (ser, n)


@need_clang
@pytest.mark.parametrize("src", ["iflive.c", "iflive_ffsk.c"])
def test_live_module_builds_fits_level_2(src):
    path = os.path.join(K6, "mod", src)
    code, entry = k6mod.build(path)
    assert k6mod.source_level(path) == 2 and k6mod.source_opt(path) == "z"
    assert 16 + len(code) + 8 <= 4096, len(code)        # the K6's 4 KB message store


@need_clang
@pytest.mark.parametrize("mode,cn0", [("4fsk", 47.0), ("ffsk", 45.0)])
def test_live_module_on_m0(mode, cn0):
    """two 3.5 s steps (a 1.5 s lead each, then PN9) 164 Hz off: both lock and decode, no
    counts lost, CPU to spare, the stack well inside the 3.5 KB the K6 has at a module's call"""
    pytest.importorskip("unicorn")
    import k6_ifcap as ic
    import k6_live_emu as emu
    ret, said, used, writes = emu.run_live(mode, cn0, -164.0, steps=2, seg_s=3.5, lead_s=1.5)
    head, steps = ic.parse_live("K6 MOD say " + s for s in said)
    assert ret == 2, said
    assert 5480 <= head["ks"] <= 5500, said
    assert len(steps) == 2, said
    for d in steps:
        assert d["s"] == 2 and d["L"] == 0, said         # locked, nothing lost
        assert d["b"] > 300 and d["e"] / 3 / d["b"] < 0.01, said
        assert d["i"] > 20, said                         # idle %
        assert abs(d["f"] * 65536 / 50069 + 164) < 60, said
    assert used < 3000, used
    assert writes[0x7E][-1] == 0 and writes[0x34][0] & 15 == 13, writes  # put back; code 13
