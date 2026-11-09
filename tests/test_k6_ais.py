"""AIS on the K6 (docs/k6-ais.md): the message parser against a known sentence, the receiver
(sim/k6if/ais.py) on bursts through the GP0 model, and the aiscap module (c/k6/mod/aiscap.c) on
an emulated Cortex-M0 against the modelled DMA stream (tests/k6_live_emu.py): it must record
each burst whole, head included, for the host's receiver to decode.
SPDX-License-Identifier: MIT"""
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
from sim.k6if import ais, dmr, frontend as fe, study as st  # noqa: E402

need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")
CAP = os.path.join(K6, "mod", "aiscap.c")


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
    code, _ = k6mod.build(CAP)
    assert len(code) + 24 + 1024 + 3456 + 8 <= 11776, len(code)   # the voice images' arena


@need_clang
def test_capture_module_records_bursts():
    """two bursts 0.9 s apart (a dump takes ~0.5 s on the cable): aiscap records each whole (its head from before the RSSI rose)
    and the host decodes both; nothing big on the stack"""
    pytest.importorskip("unicorn")
    import k6_live_emu as le
    msgs = [ais.msg4(3160001, 2026, 11, 9, 3, 15, 42),
            ais.msg1(316012345, 43, ais.sotdma(0, 2, 1712))]
    gap = np.zeros(int(0.9 * st.FZ), complex)
    parts, spans, pos = [np.zeros(int(0.6 * st.FZ), complex)], [], int(0.6 * st.FZ)
    for m in msgs:
        b = ais.gmsk(ais.burst_bits(m), f_off=300.0, pre_s=0.0, post_s=0.0)
        spans.append((pos, pos + len(b)))
        parts += [b, gap]
        pos += len(b) + len(gap)
    s = np.concatenate(parts)
    bits = fe.receive(s, st.FZ, 62.0, seed=3, L=0, fdma=fe.FDMA)
    ib = bits.astype(np.uint8) * 2
    on = [(int(a / st.FZ * fe.FDMA), int(b / st.FZ * fe.FDMA)) for a, b in spans]
    code, entry = build([("RUN_S", "3u"), ("SWITCH_S", "1u")])
    e = le.Emu(code, entry, ib, 0, fe.FDMA, 1.5, max_s=3.5, scratch=8192)
    # the RSSI rises 1.5 ms into the burst (the chip's settling) and falls at its end
    e.rssi = lambda: -70 if any(a + 8000 <= e.sample_now() < b for a, b in on) else -118
    r = e.run()
    assert r is not None
    ret, stack = r
    txt = "\n".join(ln for ln in e.said if not ln.startswith("y "))
    print(txt)
    caps = dmr.parse_caps(e.said)
    assert ret == len(caps) == 2, txt
    got = []
    for c in caps:
        assert len(c["x"]) == 3456 and c["lost"] == 0, txt
        got += [g[0] for g in ais.receive(c["x"], fc=c["ks"] * 1000.0 / fe.L)]
    assert got == msgs
    assert any(ln.startswith("m ") for ln in e.said), txt
    assert e.writes[0x13][-1] == 0 and e.writes[0x7E][-1] == 0, e.writes   # the AGC put back
    assert stack < 600, stack
