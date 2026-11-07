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


@pytest.fixture(scope="module")
def lib4h():
    """the receiver built for four tones on a half-baud grid, one library a baud rate"""
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    d, out = tempfile.mkdtemp(), {}
    for b in (1200, 1500, 1600):
        so = os.path.join(d, f"libifrx_h{b}.so")
        subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-DIFRX_ONLY=2",
                        f"-DIFRX_BAUD={b}", os.path.join(K6, "ifrx_host.c"), "-o", so], check=True)
        L = ctypes.CDLL(so)
        L.ifrx_run.argtypes = [P, I, I, ctypes.c_uint, I, I, I, P, P, P, P, I, P, P]
        out[b] = L
    return out


def mode_of(name):
    tones, baud, lv = md.MODES[name]
    return md.Mode(tones, baud, lv)


def run(L, c, m, ks=5490, start=0, pause=0, every=0, maxsym=20000, noise=(0, 0)):
    """-> symbols, gap flags, next symbol starts (counts), info. noise: Gaussian noise (Q12
    rms) on the timing and AFC detectors' outputs"""
    c = np.ascontiguousarray(np.clip(c, -127, 127).astype(np.int8))
    sym, met = np.zeros(maxsym, np.uint8), np.zeros(maxsym * 4, np.uint32)
    afc, gap = np.zeros(maxsym, np.int32), np.zeros(maxsym, np.uint8)
    info, taus = np.zeros(10, np.int32), np.zeros(maxsym, np.uint32)
    info[9] = (noise[0] << 16) | noise[1]
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


@pytest.mark.parametrize("baud,cn0,limit", [(1200, 46, 0.004), (1500, 46, 0.007), (1600, 46, 0.012),
                                             (1600, 48, 0.002)])
@pytest.mark.parametrize("off", [0, -164])
def test_faster_4fsk_on_the_model(lib4h, baud, cn0, off, limit):
    """four tones on a half-baud grid (IFRX_ONLY = IFRX_4H, IFRX_BAUD): 12 samples a symbol at
    1500 and 1600 baud, one template a tone. At 46 dB-Hz 4fsk1200 makes about 0.8% (the test
    above at 47 holds it under that), 1200 baud on this grid 0.2%, 1500 0.35%, 1600 0.8%; a
    1.5 s lead, 164 Hz off; and no lock on a bare carrier"""
    m = mode_of(f"4h{baud}")
    lk = mcu.Link(m, 2400, lead=int(1.5 * baud), f_off=off, pre=0.3, seed=3)
    sym, gap, taus, info = run(lib4h[baud], lk.counts(cn0, seed=5), m, start=int(0.02 * mcu.FC))
    assert info[5] == 2 and info[3] == 0, info
    assert ber(lk, sym) < limit
    if off == 0 and cn0 == 46:
        lk = mcu.Link(m, 200, lead=2, pre=3.0, seed=4)
        sym, gap, taus, info = run(lib4h[baud], lk.counts(55, seed=55)[:int(2.9 * mcu.FC)], m)
        assert len(sym) == 0 and info[2] >= 6, info


# the detectors' noise on Jeff's K6 (tests/data/k6/gp0cap-20261106.log.gz, steps 1-8, at the
# data-aided timing and IF): timing (|L|^2-|E|^2)/|C|^2 sd 0.76 FFSK, 0.45 4FSK; AFC
# Im(c2 c1*)/mean |c|^2 sd 0.36, 0.45. The model's at 70 dB-Hz: 0.06-0.12. White a symbol, and
# not the pattern's; the decisions there are clean (FFSK 0 errors, 4FSK 0.4%)
K6_NOISE = {"ffsk1200": (3100, 1460), "4fsk1200": (1840, 1840)}


@pytest.mark.parametrize("name", ["ffsk1200", "4fsk1200"])
def test_loops_through_the_k6s_detector_noise(lib, name):
    """with that noise on the detectors, the loops hold: no false lock (the lead's timing loop
    once ran at 4x, past stable: FFSK then locked half a symbol off in 2 of 4 runs, as it did on
    the K6 on 2026-11-07), and narrow enough tracking keeps the jitter down (it was 0.50
    half-samples and 21 Hz rms for 4FSK at the old gains; worth ~1.3% BER on the captures)"""
    m = mode_of(name)
    pc = mcu.FC / m.baud
    for seed in (3, 4):
        lk = mcu.Link(m, 3000, lead=1800, pre=0.3, seed=seed)
        start = int(0.02 * mcu.FC)
        sym, gap, taus, info = run(lib, lk.counts(70, seed=seed + 7), m, start=start,
                                   noise=K6_NOISE[name])
        assert info[5] == 2 and info[3] == 0, info
        assert ber(lk, sym) < 0.001
        k = ((taus + start - pc) / mcu.FC - lk.t0) * m.baud
        fr = (k - np.round(k))[1800:]
        fr = (fr - np.angle(np.mean(np.exp(2j * np.pi * fr))) / (2 * np.pi) + 0.5) % 1 - 0.5
        assert fr.std() * 2 * mcu.TL < 0.4               # half-samples rms


@pytest.mark.parametrize("name", ["ffsk1200", "4fsk1200"])
def test_other_dma_rates(lib, name):
    """the DMA's rate moves with the CPU's load (5490 .. 5875 kS/s on Jeff's K6): told the
    rate, the receiver works at any of them; told one 1% off, it doesn't lock (iflive measures
    the rate as it runs for this)"""
    m = mode_of(name)
    for f in (5.40e6, 5.875e6):
        lk = mcu.Link(m, 1500, lead=1800, f_off=-164, pre=0.3, seed=3, fdma=f)
        c = lk.counts(50, seed=5)
        sym, gap, taus, info = run(lib, c, m, ks=round(f / 1000), start=int(0.02 * f / 64))
        assert info[5] == 2 and info[3] == 0 and ber(lk, sym) < 0.005, (f, info)
        sym, gap, taus, info = run(lib, c, m, ks=round(f / 1000 * 1.01), start=int(0.02 * f / 64))
        assert info[5] != 2 or len(sym) < 200, (f, info)


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
@pytest.mark.parametrize("src", ["iflive.c", "iflive_ffsk.c", "iflive_tsweep.c", "iflive_fsweep.c", "iflive_g2.c", "iflive_k6t.c",
                                 "iflive_h1200.c", "iflive_h1500.c", "iflive_h1600.c"])
def test_live_module_builds_fits_level_2(src):
    path = os.path.join(K6, "mod", src)
    code, entry = k6mod.build(path)
    assert k6mod.source_level(path) == 2 and k6mod.source_opt(path) == "z"
    assert 16 + len(code) + 8 <= 4096, len(code)        # the K6's 4 KB message store


@need_clang
@pytest.mark.parametrize("mode,cn0,fdma,stall", [("4fsk", 47.0, None, None), ("ffsk", 45.0, None, None),
                                                 ("4fsk", 50.0, 5.875e6, (40, 400)), ("4h1600", 47.0, None, None),
                                                 ("4h1500", 50.0, 5.875e6, (40, 400))])
def test_live_module_on_m0(mode, cn0, fdma, stall):
    """two 3.5 s steps (a 1.5 s lead each, then PN9) 164 Hz off: both lock and decode, no
    counts lost, CPU to spare, the stack well inside the 3.5 KB the K6 has at a module's call.
    The module starts on KS0 (5530 kS/s) and must measure the DMA's real rate: the model's
    5490, and 5875 (what the K6 polled at on 2026-11-06), there with a 400 us stall every 40
    key checks (a lap of the ring missed: counts lost, never the clock)"""
    pytest.importorskip("unicorn")
    import k6_ifcap as ic
    import k6_live_emu as emu
    ret, said, used, writes = emu.run_live(mode, cn0, -164.0, steps=2, seg_s=3.5, lead_s=1.5,
                                           fdma=fdma, stall=stall)
    head, steps = ic.parse_live("K6 MOD say " + s for s in said)
    assert ret == 2, said
    assert len(steps) == 2, said
    for d in steps:
        assert abs(d["k"] - (fdma or mcu.FDMA) / 1000) <= 2, said   # the rate it measured
        assert d["j"] == 0 and (d["L"] == 0 or stall), said          # no reject, nothing lost
        assert d["b"] > 300 and d["e"] / 3 / d["b"] < 0.01, said
        assert d["i"] > 20, said                         # idle %
        assert abs(d["f"] * 65536 / 50069 + 164) < 60, said
    assert used < 3000, used
    assert writes[0x7E][-1] == 0 and writes[0x34][0] & 15 == 13, writes  # put back; code 13
