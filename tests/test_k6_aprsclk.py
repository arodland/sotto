"""The K6 APRS clock module (c/k6/mod/aprsclk.c, docs/k6-aprs-clock.md).

APRS packets (modem/aprs.py: AX.25 UI frames as Bell 202 AFSK) go through the study's model of
the K6's receive path (sim/k6if: FM, the FD6818's filter and AGC, the GP0 code 13 sigma-delta at
the DMA's rate). The receiver after the DMA is built for this machine (c/k6/aprsclk_host.c) for
the demodulator's margins and the parser's and the clock vote's cases; then the whole module runs
on an emulated Cortex-M0 against the modelled DMA stream (tests/k6_live_emu.py): it must decode
every packet, set the clock within a second of the truth with no counts lost, keep its CPU margin,
and stay inside the arena and the stack. Needs a C compiler; the module test also clang, ld.lld
and unicorn. SPDX-License-Identifier: MIT"""
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
SRC = os.path.join(K6, "mod", "aprsclk.c")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modem import aprs  # noqa: E402

need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")
DAY = 86400.0
UTC0 = 8 * DAY + 17 * 3600 + 52 * 60 + 0.25   # the true UTC at the module's start (day 8, 17:52:00.25)
FDMA = 5.49e6
CPS8 = int(round(96e6 * 256 / FDMA))


@pytest.fixture(scope="module")
def lib():
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    so = os.path.join(tempfile.mkdtemp(), "libaprsclk.so")
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-I" + K6,
                    os.path.join(K6, "aprsclk_host.c"), "-o", so], check=True)
    L = ctypes.CDLL(so)
    L.aprs_counts.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    L.aprs_frame.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32]
    L.aprs_lines.restype = ctypes.c_char_p
    return L


def result(L):
    r = (ctypes.c_int32 * 9)()
    L.aprs_result(r)
    return dict(zip("votes wid utc0 ok bad nts day mon yr".split(), r))


def truth_ms():
    return round((UTC0 % DAY) * 1000)


# ---- the traffic: three stations, each stamping a little before it keys ----
def packets():
    """[(start s, frames for the UTC at the first bit)]: a GPS tracker's HHMMSSh, a weather
    station's DDHHMMz through a digipeater, and a raw $GPRMC"""
    return [
        (0.5, lambda u: [aprs.frame("VE7ABC-9", "APRS", "/" + aprs.ts_hms(u - 0.4) + "4857.00N/12229.00W>hi")]),
        (2.6, lambda u: [aprs.frame("VE7WX", "APRS", "@" + aprs.ts_dhm(u - 20) + "4857.00N/12229.00W_090/000g000t050",
                                    path=("WIDE2-1",), repeated=1)]),
        (4.4, lambda u: [aprs.frame("N0CALL-7", "GPS", aprs.rmc(u - 0.7))]),
    ]


def scene(cn0=55.0, f_off=0.0, twist=0.0, dev=3000.0, seed=1, total=6.5, fdma=FDMA):
    """the DMA's bytes (bit 1 = PB1) for the packets on 144.390: dev is the louder tone's
    deviation (Hz), twist the space tone over the mark (dB)"""
    from sim.k6if import frontend as fe, study as st
    a = np.zeros(int(total * st.FSA))
    for t, mk in packets():
        x, t0 = aprs.packet_audio(mk(UTC0 + t + 40 * 8 / aprs.BAUD), fs=st.FSA, twist_db=twist)
        i = int(t * st.FSA)
        a[i:i + len(x)] += x
    s = st.fm(a * dev / max(1.0, 10 ** (twist / 20)), f_off)
    bits = fe.receive(s, st.FZ, cn0, bw=8000.0, seed=seed, L=0, fdma=fdma)
    return bits.astype(np.uint8) * 2


def counts(ib):
    b = (ib // 2).astype(np.int32)
    n = len(b) // 64
    return np.ascontiguousarray((2 * b[:n * 64].reshape(n, 64).sum(1) - 64).astype(np.int8))


def host_run(L, ib, cps8=CPS8):
    c = counts(ib)
    L.aprs_reset(cps8)
    L.aprs_counts(c.ctypes.data, len(c), 0)
    return result(L), L.aprs_lines().decode()


# ---- the demodulator, on the host ----
def test_decodes_every_packet_and_sets_the_clock(lib):
    r, lines = host_run(lib, scene())
    assert r["ok"] == 3 and r["nts"] == 3, lines
    assert r["votes"] == 3
    # the estimate leans on the latest stamp: within 0.5 s of the truth, never more than 1 s off
    assert abs(r["utc0"] - truth_ms()) < 500, (r, truth_ms())
    assert (r["day"], r["mon"], r["yr"]) == (8, 11, 26)
    assert "ts=17:52:00h" in lines and "ts=17:51:00z" in lines and "ts=17:52:04.210r" in lines


@pytest.mark.parametrize("kw", [dict(twist=6.0), dict(twist=-4.0), dict(f_off=800.0), dict(f_off=-800.0),
                                dict(dev=2000.0), dict(dev=4500.0), dict(cn0=53.0, seed=2)])
def test_margins(lib, kw):
    """a sender's pre-emphasis (space tone louder) or none, a carrier off frequency (the chip's
    AFC pulls it in on the radio, not here), low and high deviation, a weak signal"""
    r, lines = host_run(lib, scene(**kw))
    assert r["ok"] == 3, (kw, lines)
    assert abs(r["utc0"] - truth_ms()) < 500


def test_bench_loopback_packets(lib):
    """the on-air loopback's audio (modem/aprs.py wav --now, sent by the IC-7100 to
    aprsclk_440): three AG7EW stations, each stamped with the true UTC at its own first bit"""
    from sim.k6if import frontend as fe, study as st
    x, sent = aprs.bench_wav(UTC0 + 0.5, gap=0.3, fs=st.FSA)
    assert [c for c, _, _ in sent] == ["AG7EW-1", "AG7EW-2", "AG7EW-3"]
    a = np.zeros(int(0.5 * st.FSA) + len(x) + int(0.5 * st.FSA))
    a[int(0.5 * st.FSA):][:len(x)] = x
    s = st.fm(a * 3000.0, 0.0)
    ib = fe.receive(s, st.FZ, 55.0, bw=8000.0, seed=4, L=0, fdma=FDMA).astype(np.uint8) * 2
    r, lines = host_run(lib, ib)
    assert r["ok"] == 3 and r["nts"] == 3 and r["votes"] == 3, lines
    # exact stamps put the interval's low edge on the truth; the estimate adds the half second a
    # real station's stamp leads its keying (lo + min(w/2, 0.5 s)), so the loopback reads +0.5 s
    assert 450 < r["utc0"] - truth_ms() < 550, (r, truth_ms())
    assert r["day"] == 9  # UTC0 counts days from the epoch: 9 Jan 1970


def test_noise_alone_sets_nothing(lib):
    from sim.k6if import frontend as fe, study as st
    s = np.full(int(4 * st.FZ), 1e-9, complex)  # no carrier: the receiver's own noise
    bits = fe.receive(s, st.FZ, 40.0, bw=8000.0, seed=3, L=0, fdma=FDMA)
    r, lines = host_run(lib, bits.astype(np.uint8) * 2)
    assert r["ok"] == 0 and r["votes"] == 0 and "clk" not in lines


# ---- the parser and the vote, frames handed straight to it ----
def feed(L, items):
    """items: (module ms at the frame's end, frame). nbits: the frame's own bits, unstuffed"""
    L.aprs_reset(CPS8)
    for ms, f in items:
        L.aprs_frame(f, len(f), int(ms), len(f) * 8)
    return result(L), L.aprs_lines().decode()


def at(ms, src, info, path=(), repeated=0, dst="APRS"):
    return ms, aprs.frame(src, dst, info, path=path, repeated=repeated)


def tod(utc_s):
    return aprs.ts_hms(utc_s)


def utc_at(ms_end, f):
    """the true UTC when this frame's first bit went out"""
    return UTC0 + ms_end / 1000 - (len(f) * 8 + 8) / aprs.BAUD


def test_one_station_is_not_enough(lib):
    ms = 3000
    f = aprs.frame("VE7ABC-9", "APRS", "/" + tod(UTC0 + 2.5) + "4857.00N/12229.00W>")
    r, lines = feed(lib, [(ms, f)])
    assert r["votes"] == 1 and r["utc0"] == -1 and "clk --:--:--" in lines


def test_a_digipeated_copy_is_one_vote(lib):
    f = aprs.frame("VE7ABC-9", "APRS", "/" + tod(UTC0 + 1.0) + "4857.00N/12229.00W>", path=("WIDE1-1",))
    g = aprs.frame("VE7ABC-9", "APRS", "/" + tod(UTC0 + 1.0) + "4857.00N/12229.00W>", path=("WIDE1-1",),
                   repeated=1)
    r, _ = feed(lib, [(1800, f), (2900, g)])
    assert r["votes"] == 1 and r["nts"] == 1


def test_marzullo_outvotes_a_wrong_clock(lib):
    """three GPS stations agree; one with its clock 7 minutes slow loses"""
    items = []
    for k, (call, slow) in enumerate([("AA7AA", 0), ("BB7BB", 420), ("CC7CC", 0), ("DD7DD", 0)]):
        ms = 2000 + 1500 * k
        info = "/" + tod(UTC0 + ms / 1000 - 0.6 - slow) + "4857.00N/12229.00W>"
        items.append((ms, aprs.frame(call, "APRS", info)))
    r, lines = feed(lib, items)
    assert r["votes"] == 3, lines
    assert abs(r["utc0"] - truth_ms()) < 1000


def test_local_time_and_bad_stamps_are_ignored(lib):
    items = [
        at(1000, "AA7AA", "/081052/4857.00N/12229.00W>local time"),     # DDHHMM/ (local)
        at(1500, "BB7BB", "@992575z4857.00N/12229.00W_"),               # day 99, hour 25
        at(2000, "CC7CC", "$GPRMC,175201,V,4857.00,N,12229.00,W,0,0,081126,,*00"),  # no fix
        at(2500, "DD7DD", "!4857.00N/12229.00W>no time"),
        at(3000, "EE7EE", "}FF7FF>APRS,TCPIP,EE7EE*:/175202h4857.00N/12229.00W>"),  # third party
    ]
    r, lines = feed(lib, items)
    assert r["ok"] == 5 and r["nts"] == 0, lines


def test_objects_and_status_carry_time(lib):
    t1, t2 = 4000, 6000
    o = aprs.frame("VE7DIG", "APRS", ";LEAVENWTH*" + aprs.ts_hms(UTC0 + t1 / 1000 - 1.0) + "4857.00N/12229.00W-")
    s = aprs.frame("VE7XYZ", "APRS", ">" + aprs.ts_dhm(UTC0 + t2 / 1000 - 1.0) + "on the air")
    r, lines = feed(lib, [(t1, o), (t2, s)])
    assert r["nts"] == 2 and r["votes"] == 2, lines
    assert abs(r["utc0"] - truth_ms()) < 1500
    assert r["day"] == 8


def test_midnight(lib):
    """stamps either side of 00:00 UTC agree (offsets are taken round the day)"""
    global UTC0
    old = UTC0
    try:
        UTC0 = 9 * DAY - 2.0                    # 23:59:58 on the 8th
        a = aprs.frame("AA7AA", "APRS", "/" + tod(UTC0 + 1.0 - 0.3) + "4857.00N/12229.00W>")   # 23:59:58
        b = aprs.frame("BB7BB", "APRS", "/" + tod(UTC0 + 4.0 - 0.3) + "4857.00N/12229.00W>")   # 00:00:01
        r, lines = feed(lib, [(1000 + len(a) * 8 * 5 // 6, a), (4000 + len(b) * 8 * 5 // 6, b)])
        assert r["votes"] == 2, lines
        assert abs((r["utc0"] - truth_ms() + 43200000) % 86400000 - 43200000) < 1000
    finally:
        UTC0 = old


def test_float_decoder_reads_test_audio():
    """modem.aprs's own float decoder (for recordings) reads what it makes"""
    fr = [aprs.frame("VE7ABC-9", "APRS", "/175201h4857.00N/12229.00W>hi", path=("WIDE1-1",), repeated=1),
          aprs.frame("N0CALL", "APRS", aprs.rmc(64321.5))]
    x, _ = aprs.packet_audio(fr)
    got = [aprs.describe(f) for f, _ in aprs.demod(x, aprs.FS)]
    assert got == ["VE7ABC-9>APRS,WIDE1-1*:/175201h4857.00N/12229.00W>hi",
                   "N0CALL>APRS:" + aprs.rmc(64321.5)]


# ---- the whole module on the emulated M0 ----
@need_clang
def test_module_on_the_emulated_m0():
    pytest.importorskip("unicorn")
    import k6_live_emu as le
    from modem import k6mod
    total = 6.5
    code, entry = le.build("x", [("RUN_MS", f"{int(total * 1000)}u")], SRC)
    full, _ = k6mod.build(SRC)
    # the arena of the SPI images (11,776 B less the header and tag) holds the code and the state
    st = 1024 + 332 + 4 * 16 * 4 + 12 * 28 + 64 + 200
    assert len(full) + 24 + st <= 11776
    ib = scene(total=total)
    for cpi in (1.5, 4.0):                       # the calibrated cost, then 2.7x it: no counts lost
        e = le.Emu(code, entry, ib, 0, FDMA, cpi, max_s=total + 1, level=7, scratch=11776 - 24 - len(full))
        r = e.run()
        assert r is not None, e.said
        ret, stack = r
        assert ret == 3, e.said
        assert stack < 700, stack                # modules get about 700 B
        end = e.said[-1]
        assert "ok=3 bad=0 ts=3 v=3" in end and "lost=0" in end, e.said
        # the end line's UTC is at the module's last ms (~6500)
        hh, mm, ss = end.split("utc=")[1].split()[0].split(":")
        got = (int(hh) * 3600 + int(mm) * 60 + float(ss))
        want = (UTC0 + total) % DAY
        assert abs(got - want) < 0.6, (end, want)
    assert e.lcd[1].startswith("UTC 17:52:0") and "stations 3 (sec)" in e.lcd[2], e.lcd
    assert any(on for _, on in e.led), "the LED blinks a good frame"
    assert e.writes.get(0x34, [None])[-1] == 0 and e.writes.get(0x33)  # GP0 put back
