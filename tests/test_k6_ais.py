"""AIS on the K6 (docs/k6-ais.md): the message parser against a known sentence, the receiver
(sim/k6if/ais.py) on bursts through the GP0 model, and the aiscap module (c/k6/mod/aiscap.c) on
an emulated Cortex-M0 against the modelled DMA stream (tests/k6_live_emu.py): it must record
each burst whole, head included, for the host's receiver to decode.
SPDX-License-Identifier: MIT"""
import gzip
import os
import shutil
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
from sim.k6if import ais, frontend as fe, study as st  # noqa: E402

need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")
CAP = os.path.join(K6, "mod", "aiscap.c")


def test_aivdm_from_serial_log():
    """tools/k6_aivdm.py takes the sentence out of a logged say line, checking its checksum"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_aivdm
    ln = "1794200019.4\taf064d6\tK6 MOD say !AIVDM,1,1,,B,15M:Ih000lo>j20Ks7072?wB0<09,0*27"
    assert k6_aivdm.sentence(ln) == "!AIVDM,1,1,,B,15M:Ih000lo>j20Ks7072?wB0<09,0*27"
    assert k6_aivdm.sentence(ln[:-1] + "8") is None
    assert k6_aivdm.sentence("K6 MOD say a 1 366123456 B r=18") is None


def test_parse_known_sentences():
    """gpsd's AIVDM guide: a base station report (message 4) and a class A position"""
    d = ais.parse(ais.nmea_bits("403OviQuMGCqWrRO9>E6fE700@GO"))
    assert d["type"] == 4 and d["mmsi"] == 3669702
    assert (d["year"], d["month"], d["day"], d["hour"], d["minute"], d["second"]) == (2007, 5, 14, 19, 57, 39)
    assert d["comm"] == {"sync": 0, "timeout": 4, "slot": 1503}
    d = ais.parse(ais.nmea_bits("15M67FC000G?ufbE`FepT@3n00Sa"))
    assert d["type"] == 1 and d["mmsi"] == 366053209 and d["second"] == 59


def test_comm_state_hour_minute():
    m = ais.msg1(316001234, 17, ais.sotdma(0, 1, (21 << 9) | (34 << 2)))
    assert ais.parse(m)["comm"] == {"sync": 0, "timeout": 1, "hour": 21, "minute": 34}


def test_crc_residue():
    air = ais.to_air(ais.msg4(1, 2026, 11, 9, 3, 15, 42))
    assert ais.crc_ok(air + ais.fcs_bits(air))
    bad = list(air)
    bad[5] ^= 1
    assert not ais.crc_ok(bad + ais.fcs_bits(air))


def test_receive_clean_and_timing():
    """a clean burst decodes, and the start flag's time is within a bit (it comes out ~0.6 bit
    late: the chip filter's and the CIC's delay, a constant the bench calibrates)"""
    m = ais.msg4(3669702, 2026, 11, 9, 3, 15, 42)
    pre = 0.004
    x = ais.to_counts(ais.gmsk(ais.burst_bits(m), pre_s=pre), None)
    got = ais.receive(x)
    assert [g[0] for g in got] == [m]
    t_flag = pre + ais.FLAG_OFF_BITS / ais.BAUD
    assert 0 <= got[0][1] / ais.FC - t_flag < 1.0 / ais.BAUD


def test_receive_noise_offset_rate():
    """58 dB-Hz with the carrier 600 Hz off the IF and the DMA's rate 0.2% off: most decode"""
    rng = np.random.default_rng(4)
    ok = 0
    for k in range(10):
        m = ais.msg1(316000000 + k, int(rng.integers(0, 60)), ais.sotdma(0, 2, int(rng.integers(0, 2250))))
        s = ais.gmsk(ais.burst_bits(m), f_off=600.0 * (-1) ** k, t_frac=float(rng.uniform(0, 20)))
        x = ais.to_counts(s, 58.0, seed=k + 1)
        got = ais.receive(x, fc=ais.FC * (1 + 0.002 * (-1) ** k))
        ok += any(g[0] == m for g in got)
    assert ok >= 8, ok


# ---- aiscap on the emulated M0 ----

def build(defs, src=CAP):
    with tempfile.TemporaryDirectory() as d:
        w = os.path.join(d, "w.c")
        with open(w, "w") as f:
            f.writelines(f"#define {k} {v}\n" for k, v in defs)
            f.write(f'#include "{src}"\n')
        return k6mod.build(w)


@need_clang
def test_module_fits():
    """code + header + tag + the 3,600-count ring in the voice images' 11,776-byte arena"""
    code, _ = k6mod.build(CAP)
    assert len(code) + 24 + 3600 + 128 <= 11776, len(code)


def poll_stream(s, cn0, seed=3, fdma=22.9e6, g=32):
    """the 1-bit stream the polling loop reads (at fdma) for complex baseband s, as cumulative
    ones at every g-th sample (the emulator's poll) and that grid's rate"""
    c = fe.receive(s, st.FZ, cn0, seed=seed, fdma=fdma, L=g, floor=0.01)
    return np.concatenate([[0], np.cumsum((c + g) // 2)]), fdma / g


@need_clang
def test_capture_module_records_bursts():
    """two bursts 1.0 s apart on the polled path: aiscap calibrates on the empty channel, records
    each burst whole (its head from before the energy rose) and the host decodes both, at the
    count rate the module measured. Nothing big on the stack. The model's floor 0.01 is the bench's: the 1-bit path's noise over the
    receiver's (docs/k6-ais.md, run 1)"""
    pytest.importorskip("unicorn")
    import k6_live_emu as le
    msgs = [ais.msg4(3160001, 2026, 11, 9, 3, 15, 42),
            ais.msg1(316012345, 43, ais.sotdma(0, 2, 1712))]
    s = np.zeros(int(3.2 * st.FZ), complex)
    for m, t in zip(msgs, (0.9, 1.9)):
        b = ais.gmsk(ais.burst_bits(m), f_off=-1300.0, pre_s=0.0, post_s=0.0)
        i = int(t * st.FZ)
        s[i:i + len(b)] += b
    cs, fg = poll_stream(s, 66.0)
    code, entry = build([("RUN_S", "3u"), ("SWITCH_S", "5u"), ("AISCAP_EMU", "1")])
    e = le.Emu(code, entry, np.zeros(16, np.uint8), 0, fe.FDMA, 1.5, max_s=3.4, scratch=4096)
    e.poll, e.poll_fc, e.poll_k = cs, fg, 8
    fc = fg / 8
    r = e.run()
    assert r is not None
    ret, stack = r
    txt = "\n".join(ln for ln in e.said if not ln.startswith("y "))
    print(txt)
    caps = ais.parse_caps(e.said)
    assert ret == len(caps) == 2, txt
    f_line = next(ln for ln in e.said if ln.startswith("f B hz="))
    assert 0.9 < int(f_line.split("hz=")[1].split()[0]) / fc < 1.0, f_line   # its cycles between counts
    got = []
    for c in caps:
        assert len(c["x"]) == 3600, txt
        got += [g[0] for g in ais.receive_cap(c)]
    assert got == msgs
    assert any(ln.startswith("m B") for ln in e.said), txt
    assert e.writes[0x13][-1] == 0 and e.writes[0x7E][-1] == 0, e.writes   # the AGC put back
    assert stack < 600, stack


def test_air_run2_decodes():
    """run 2 (2026-11-09 03:16Z, polled aiscap on the K6, receive only): the host decodes the shore
    base station 003669705's reports from the captures 12 dB or more over the floor, and each
    report's slot puts its burst in the same UTC second it states"""
    p = os.path.join(ROOT, "tests", "data", "k6", "air", "aiscap2-20261109T0315Z.log.gz")
    with gzip.open(p, "rt", errors="replace") as fh:
        caps = [c for c in ais.parse_caps(fh) if c["rssi"] >= 12]
    frames = [ais.parse(m) for c in caps for m, _ in ais.receive_cap(c)]
    t4 = [d for d in frames if d["type"] == 4]
    assert len(frames) >= 9 and len(t4) >= 6, frames
    assert all(d["mmsi"] == 3669705 and (d["year"], d["month"], d["day"], d["hour"]) == (2026, 11, 9, 3)
               for d in t4)
    slots = [d for d in t4 if "slot" in d["comm"]]
    assert len(slots) >= 3
    for d in slots:
        assert int(d["comm"]["slot"] * 60 / 2250 + ais.FLAG_OFF_BITS / ais.BAUD) == d["second"], d


# ---- the receiver on the M0 (c/k6/aisrx.h) and aisclk ----

CLK = os.path.join(K6, "mod", "aisclk.c")
ARENA = 11776                   # the voice images' module arena
TLM = 696                       # its telemetry text at the end (arena::TLM_BYTES), when asked for


def clk_scratch():
    """the scratch the radio gives aisclk: the arena past its header, code and tag, less the
    telemetry text (from the radio's build: the emulator's, AISCAP_EMU, is bigger)"""
    code, _ = k6mod.build(CLK)
    return (ARENA - 16 - len(code) - 8 - TLM) & ~3


def rx_lib():
    import ctypes
    import subprocess
    if not shutil.which("cc"):
        pytest.skip("no cc")
    d = tempfile.mkdtemp()
    so = os.path.join(d, "aisrx.so")
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-I" + K6,
                    os.path.join(K6, "aisrx_host.c"), "-o", so], check=True)
    return ctypes.CDLL(so)


def rx_decode(lib, c, if_hz=None):
    """aisrx.h's ar_decode on one capture -> [(message bits, flag in counts from its start)];
    with if_hz, ar_decode_if with the burst there"""
    import ctypes
    x = np.ascontiguousarray(c["x"], np.int8)
    out = (ctypes.c_uint8 * (8 * 48))()
    if if_hz is None:
        k = lib.ar_host(x.ctypes.data_as(ctypes.c_void_p), len(x), c["pre"], c["c1"], out, 8)
    else:
        k = lib.ar_host_if(x.ctypes.data_as(ctypes.c_void_p), len(x), c["pre"], c["c1"],
                           int(round(if_hz * 2 ** 32 / 96e6)), out, 8)
    assert k >= 0
    b = bytes(out)
    got = []
    for i in range(k):
        r = b[48 * i:48 * (i + 1)]
        nb = int.from_bytes(r[:4], "little")
        got.append(([(v >> (7 - j)) & 1 for v in r[8:8 + nb] for j in range(8)],
                    int.from_bytes(r[4:8], "little") / 65536))
    return got


def test_m0_receiver_both_channels_run6():
    """run 6 (2026-11-09 05:44Z, aiscap_dual tuned to 162.009 MHz, receive only): one burst at a
    time, as the ship list module's 2,960-count ring holds it, the M0 receiver decodes channel A
    at 17.1 kHz in the counts (mirrored) and channel B at 32.9 kHz. Each capture held the base
    station's A burst at the trigger and its B burst 53.3 ms later; 30 of the 32 are on its
    cadence (the host's receiver, trying 5 IFs each: 26 A, 15 B)"""
    p = os.path.join(ROOT, "tests", "data", "k6", "air", "dual-20261109T0543Z.log.gz")
    with gzip.open(p, "rt", errors="replace") as fh:
        caps = ais.parse_caps(fh)
    lib = rx_lib()

    def window(c, a):
        w = dict(c, x=np.asarray(c["x"][a:a + 2960]), pre=384)
        w["c1"] = int(round(c["c1"] * (2960 - 384) / (len(c["x"]) - c["pre"])))
        return w

    got = {"A": 0, "B": 0}
    for c in caps:
        fc = 96e6 * (len(c["x"]) - c["pre"]) / c["c1"]
        b0 = int(53.3e-3 * fc) + c["pre"] - 384 - 100  # B's flag 53.3 ms after A's, less a margin
        for ch, hz, a in (("A", ais.CH_A, 0), ("B", ais.CH_B, b0)):
            fr = rx_decode(lib, window(c, a), ais.tap_hz(hz - 162009000, fc))
            for m, _ in fr:
                assert ais.parse(m)["mmsi"] == 3669705
            got[ch] += bool(fr)
    print(got)
    assert got["A"] >= 11 and got["B"] >= 17, got


def test_m0_receiver_on_air_run2():
    """the integer receiver decodes run 2's base station frames as the host's does (9 of the 10:
    the host's tenth comes only from its whole-capture grid) and puts their flags within a count
    of the host's"""
    import gzip
    lib = rx_lib()
    p = os.path.join(ROOT, "tests", "data", "k6", "air", "aiscap2-20261109T0315Z.log.gz")
    with gzip.open(p, "rt", errors="replace") as fh:
        caps = [c for c in ais.parse_caps(fh) if c["rssi"] >= 12]
    n = 0
    for c in caps:
        ref = {tuple(m): i for m, i in ais.receive_cap(c)}
        for m, i in rx_decode(lib, c):
            assert tuple(m) in ref
            assert abs(i - ref[tuple(m)]) < 1.5, (i, ref[tuple(m)])
            assert ais.parse(m)["mmsi"] == 3669705
            n += 1
    assert n >= 9, n


def test_m0_receiver_model():
    """bursts through the polled GP0 model at 62 dB-Hz, 1300 Hz under the IF (run 1's offset)"""
    lib = rx_lib()
    rng = np.random.default_rng(7)
    ok = 0
    for k in range(8):
        m = ais.msg1(316000000 + k, int(rng.integers(0, 60)), ais.sotdma(0, 2, int(rng.integers(0, 2250))))
        b = ais.gmsk(ais.burst_bits(m), f_off=-1300.0, pre_s=0.0, post_s=0.0, t_frac=float(rng.uniform(0, 20)))
        s = np.zeros(int(0.06 * st.FZ), complex)
        i0 = int(0.012 * st.FZ)
        s[i0:i0 + len(b)] += b
        cs, fg = poll_stream(s, 62.0, seed=k + 11)
        x = np.diff(cs[::8]) - 128                         # counts of 256 reads
        fc = fg / 8
        pre = int(0.0115 * fc)
        x = x[:3600].astype(np.int8)
        c1 = int(round(96e6 * (len(x) - pre) / fc))
        ok += any(g[0] == m for g in rx_decode(lib, {"x": x, "pre": pre, "c1": c1}))
    assert ok >= 6, ok


@need_clang
def test_clock_module_fits():
    """code + header + tag + the ring, the phases and three frames in the voice images' arena, with
    the telemetry text kept at its end (the first bench run got -8 without room for it)"""
    code, _ = k6mod.build(CLK)
    assert len(code) + 24 + TLM + 2960 + 2 * ((2576 + 256) // 3 + 2) + 3 * 48 <= ARENA, len(code)


@need_clang
def test_clock_module_sets_utc():
    """a base station's report (message 4, slot 1575: 03:15:42.0033 at its flag), a ship's
    position 38 slots later (slot 1613, second 43) and another's 20 slots after that with no slot
    number (its comm state gives the station count) on the polled path: aisclk decodes all three
    on the M0, puts each flag on its clock within 0.2 ms of where the model sent it, says UTC as
    the first fixed it, and the second (its slot) and third (snapped to the slot grid) agree"""
    pytest.importorskip("unicorn")
    import k6_live_emu as le
    t1 = 0.9
    t2 = t1 + 38 * ais.SLOT_S
    t3 = t2 + 20 * ais.SLOT_S
    msgs = [ais.msg4(3160001, 2026, 11, 9, 3, 15, 42, comm=ais.sotdma(0, 2, 1575)),
            ais.msg1(316012345, 43, ais.sotdma(0, 4, 1613)),
            ais.msg1(316054321, 43, ais.sotdma(0, 3, 17))]
    s = np.zeros(int(3.2 * st.FZ), complex)
    for m, t in zip(msgs, (t1, t2, t3)):
        b = ais.gmsk(ais.burst_bits(m), f_off=-1300.0, pre_s=0.0, post_s=0.0)
        i = int(t * st.FZ)
        s[i:i + len(b)] += b
    cs, fg = poll_stream(s, 66.0)
    code, entry = build([("RUN_S", "3u"), ("SWITCH_S", "5u"), ("AISCAP_EMU", "1")], CLK)
    e = le.Emu(code, entry, np.zeros(16, np.uint8), 0, fe.FDMA, 1.5, max_s=3.6, scratch=clk_scratch(),
               level=8)
    e.poll, e.poll_fc, e.poll_k = cs, fg, 8
    r = e.run()
    assert r is not None
    ret, stack = r
    txt = "\n".join(e.said)
    print(txt)
    assert all(len(ln) <= 60 for ln in e.said), txt
    a = [ln.split() for ln in e.said if ln.startswith("a ")]
    assert [(x[1], x[2]) for x in a] == [("4", "3160001"), ("1", "316012345"), ("1", "316054321")], txt
    assert "s=" not in " ".join(a[2]), a[2]
    flag = [(t + ais.FLAG_OFF_BITS / ais.BAUD) * 1e4 for t in (t1, t2, t3)]
    for x, f in zip(a, flag):
        assert abs(int(x[-1].split("=")[1]) - f) <= 2, (x, f)
    utc_off = (3 * 3600 + 15 * 60 + 42) * 1e4 + 1e4 * ais.FLAG_OFF_BITS / ais.BAUD - flag[0]
    clk = [(ln, cy) for ln, cy in zip(e.said, e.said_cyc) if ln.startswith("clk ")]
    assert len(clk) == 3 and ret == 3, txt
    for ln, cy in clk:
        hh, mm, ss = ln.split()[1].split(":")
        got = ((int(hh) * 60 + int(mm)) * 60 + float(ss)) * 1e4
        assert abs(got - (utc_off + cy / 9600)) <= 15, (ln, got - (utc_off + cy / 9600))
        assert " w=2 " in ln and " d=09/11" in ln
    for ln, _ in clk[1:]:
        assert abs(int(ln.split("j=")[1].split()[0])) <= 2, ln
    # each clk line hands the fix to the core clock (level 8): UTC ms now, the date, w = 2 ms
    assert [(d, w, r) for _, _, d, w, r in e.clock_sets] == [(9 | 11 << 8 | 2026 << 16, 2, 1)] * 3, e.clock_sets
    for now, ms, _, _, _ in e.clock_sets:
        assert abs(ms - (utc_off / 10 + now)) <= 2, (ms, utc_off / 10 + now)
    assert e.lcd.get(0) == "AIS CLOCK" and e.lcd.get(1, "").startswith("UTC 03:15:4"), e.lcd
    assert any(on for _, on in e.led)
    assert e.writes[0x13][-1] == 0 and e.writes[0x7E][-1] == 0
    assert stack < 700, stack


@need_clang
def test_clock_module_fills_in_base_station_slot():
    """run 6: a base station names its message 4 slot only every other time, and after a gap
    longer than SNAP_S nothing re-anchored the clock. With snapping off, a message 4 that leaves
    its slot out (2 s and 75 slots after one that named slot 1575) still gives a fine fix: the
    station's slot less its second x 37.5 is kept per channel"""
    pytest.importorskip("unicorn")
    import k6_live_emu as le
    t1 = 0.9
    t2 = t1 + 75 * ais.SLOT_S
    msgs = [ais.msg4(3160001, 2026, 11, 9, 3, 15, 42, comm=ais.sotdma(0, 2, 1575)),
            ais.msg4(3160001, 2026, 11, 9, 3, 15, 44, comm=ais.sotdma(0, 3, 17))]
    s = np.zeros(int(3.2 * st.FZ), complex)
    for m, t in zip(msgs, (t1, t2)):
        b = ais.gmsk(ais.burst_bits(m), f_off=-1300.0, pre_s=0.0, post_s=0.0)
        i = int(t * st.FZ)
        s[i:i + len(b)] += b
    cs, fg = poll_stream(s, 66.0)
    code, entry = build([("RUN_S", "3u"), ("SWITCH_S", "5u"), ("SNAP_S", "0u"), ("AISCAP_EMU", "1")], CLK)
    e = le.Emu(code, entry, np.zeros(16, np.uint8), 0, fe.FDMA, 1.5, max_s=3.6, scratch=clk_scratch(),
               level=8)
    e.poll, e.poll_fc, e.poll_k = cs, fg, 8
    r = e.run()
    assert r is not None
    txt = "\n".join(e.said)
    print(txt)
    a = [ln.split() for ln in e.said if ln.startswith("a ")]
    assert len(a) == 2 and "s=" not in " ".join(a[1]), txt
    clk = [ln for ln in e.said if ln.startswith("clk ")]
    assert r[0] == 2 and len(clk) == 2 and all(" w=2 " in ln for ln in clk), txt
    assert abs(int(clk[1].split("j=")[1].split()[0])) <= 2, clk[1]


SHIPS = os.path.join(K6, "mod", "aisships.c")


def six(text, n):
    """AIS six-bit text, padded with '@' to n characters"""
    b = []
    for ch in text.ljust(n, "@")[:n]:
        o = ord(ch)
        ais.field(b, o - 64 if o >= 64 else o, 6)
    return b


def msg24a(mmsi, name):
    b = []
    ais.field(b, 24, 6), ais.field(b, 0, 2), ais.field(b, mmsi, 30), ais.field(b, 0, 2)
    b += six(name, 20)
    assert len(b) == 160
    return b


def msg18(mmsi, lat, lon, sog=73):
    b = []
    ais.field(b, 18, 6), ais.field(b, 0, 2), ais.field(b, mmsi, 30), ais.field(b, 0, 8)
    ais.field(b, sog, 10), ais.field(b, 0, 1)
    ais.field(b, round(lon * 600000) & (2 ** 28 - 1), 28), ais.field(b, round(lat * 600000) & (2 ** 27 - 1), 27)
    ais.field(b, 900, 12), ais.field(b, 511, 9), ais.field(b, 30, 6), ais.field(b, 0, 2)
    ais.field(b, 1, 1), ais.field(b, 0, 5), ais.field(b, 0, 1), ais.field(b, 0, 20)
    assert len(b) == 168
    return b


def flat_km(lat0, lon0, lat, lon):
    import math
    dy = (lat - lat0) * 60 * 1.852
    dx = (lon - lon0) * math.cos(math.radians(lat0)) * 60 * 1.852
    return math.hypot(dx, dy), math.degrees(math.atan2(dx, dy)) % 360


@need_clang
def test_ship_module_lists_both_channels():
    """aisships tuned to 162.009 MHz with the tap's width (run 5: ~60 kHz before the channel
    filter): a base station on channel A (sets home), a class A ship on B, its name (message 24)
    on A, a class B boat on B. It decodes all four at their places in the counts, keeps three
    stations, puts each at the right distance and bearing from home, says each frame as NMEA
    that decodes back to the message, and shows the nearest on the screen"""
    pytest.importorskip("unicorn")
    import k6_live_emu as le
    home, ship, boat = (48.75, -122.50), (48.80, -122.60), (48.70, -122.45)
    msgs = [(ais.msg4(3669705, 2026, 11, 9, 3, 15, 40, lat=home[0], lon=home[1]), "A"),
            (ais.msg1(366123456, 41, ais.sotdma(0, 3, 9), lat=ship[0], lon=ship[1]), "B"),
            (msg24a(366123456, "SEA BREEZE"), "A"),
            (msg18(338765432, boat[0], boat[1]), "B")]
    s = np.zeros(int(3.2 * st.FZ), complex)
    for k, (m, ch) in enumerate(msgs):
        off = (ais.CH_A if ch == "A" else ais.CH_B) - 162009000
        b = ais.gmsk(ais.burst_bits(m), f_off=off - 1300.0, pre_s=0.0, post_s=0.0)
        i = int((0.5 + 0.6 * k) * st.FZ)
        s[i:i + len(b)] += b
    # 78 dB-Hz: the noise comes through the whole ~60 kHz too (66 is right only in 8 kHz)
    c = fe.receive(s, st.FZ, 78.0, bw=60000.0, seed=3, fdma=22.9e6, L=32, floor=0.01)
    cs, fg = np.concatenate([[0], np.cumsum((c + 32) // 2)]), 22.9e6 / 32
    code, entry = build([("RUN_S", "3u"), ("SWITCH_S", "5u"), ("AISCAP_EMU", "1")], SHIPS)
    radio, _ = k6mod.build(SHIPS)
    scratch = (ARENA - 16 - len(radio) - 8 - TLM) & ~3  # fits with the telemetry text kept
    e = le.Emu(code, entry, np.zeros(16, np.uint8), 0, fe.FDMA, 1.5, max_s=3.6, scratch=scratch,
               level=7)
    e.poll, e.poll_fc, e.poll_k = cs, fg, 8
    r = e.run()
    assert r is not None
    ret, stack = r
    txt = "\n".join(e.said)
    print(txt, e.lcd, len(radio))
    assert all(len(ln) <= 60 for ln in e.said), txt
    a = [ln.split() for ln in e.said if ln.startswith("a ")]
    assert [(x[1], x[2], x[3]) for x in a] == [("4", "3669705", "A"), ("1", "366123456", "B"),
                                               ("24", "366123456", "A"), ("18", "338765432", "B")], txt
    assert ret == 3, txt
    for x, (lat, lon) in ((a[1], ship), (a[3], boat)):
        km, brg = flat_km(*home, lat, lon)
        assert abs(float(x[5].split("=")[1]) - km) <= 0.15 and abs(int(x[6].split("=")[1]) - brg) <= 2, (x, km, brg)
    nm = [ln for ln in e.said if ln.startswith("!AIVDM")]
    assert len(nm) == 4, txt
    for ln, (m, ch) in zip(nm, msgs):
        body, cks = ln[1:].split("*")
        x = 0
        for q in body:
            x ^= ord(q)
        f = body.split(",")
        assert f[:3] == ["AIVDM", "1", "1"] and f[4] == ch and int(cks, 16) == x, ln
        assert ais.nmea_bits(f[5], int(f[6]))[:len(m)] == m, ln
    assert e.lcd.get(0) == "AIS 3 stations", e.lcd
    assert e.lcd.get(1, "").startswith("3669705") and e.lcd.get(2, "").startswith("338765432"), e.lcd
    assert e.lcd.get(3, "").startswith("SEA BREEZE"), e.lcd
    assert e.lcd.get(1, "").endswith(" 0.0 000"), e.lcd  # home itself
    assert stack < 800, stack


@need_clang
def test_clock_module_gives_up_without_ais():
    """nothing on either channel for GIVEUP_S: it says so and returns 0, for aprsclk to take over"""
    pytest.importorskip("unicorn")
    import k6_live_emu as le
    cs, fg = poll_stream(np.zeros(int(2.4 * st.FZ), complex), 66.0)
    code, entry = build([("RUN_S", "60u"), ("SWITCH_S", "1u"), ("GIVEUP_S", "2u"), ("AISCAP_EMU", "1")], CLK)
    e = le.Emu(code, entry, np.zeros(16, np.uint8), 0, fe.FDMA, 1.5, max_s=3.0, scratch=clk_scratch())
    e.poll, e.poll_fc, e.poll_k = cs, fg, 8
    r = e.run()
    assert r is not None and r[0] == 0, e.said
    assert "ais clk none" in e.said, e.said
