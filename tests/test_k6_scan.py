"""The explorer's Phase 1 sweep module (c/k6/mod/scan.c) on an emulated Cortex-M0 against a
modelled FD6818 receiver, its parameter block patched the way tools/k6_scan.py patches CI's .bin:

  - it fits: 2 m (400 bins) and a 70 cm half-band fit the scratch RAM the 4 KB store leaves;
  - the coarse pass finds carriers that come and go, the refine pass puts them on the channel,
    each transmission becomes one H line with its level, and the summary decodes;
  - a carrier that never moves is called a spur and left alone;
  - the waterfall rows show the carrier in the right column, max-pooled;
  - a key stops it, a bad block and a small scratch are refused, nothing is ever written.

The receiver model: RSSI restarts at -173 dBm on every tune and ramps to the true level as the
explorer's Phase 0 measured (wide: 22 % of the way in dB at 1 ms, all of it at 12 ms; narrow
slower), with +-1 dB of read noise. The true level is the noise floor plus every signal within
the filter (wide: 12 kHz, narrow: 6 kHz, 20 dB down at half again that). SPDX-License-Identifier: MIT"""
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, rssi_reg, run  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_scan as sw  # noqa: E402

SRC = sw.SRC
WIDE = [(0, 0.0), (1000, 0.22), (1500, 0.36), (2000, 0.46), (3000, 0.59), (5000, 0.70), (8000, 0.91),
        (12000, 1.0)]
NARROW = [(0, 0.0), (1000, 0.06), (2000, 0.18), (3000, 0.36), (5000, 0.55), (8000, 0.69), (12000, 0.89),
          (20000, 1.0)]
FLOOR = -137


def ramp(us, table):
    for (t0, g0), (t1, g1) in zip(table, table[1:]):
        if us < t1:
            return g0 + (g1 - g0) * (us - t0) / (t1 - t0)
    return 1.0


class Receiver:
    """signals: [(hz, dBm, [(from_us, to_us), ...])]"""

    def __init__(self, signals, seed=1):
        self.sig, self.f, self.t, self.wide = signals, 440_850_000, 0, 1
        self.rnd = random.Random(seed)
        self.tunes = []

    def tune(self, hz, wide, us):
        self.f, self.t, self.wide = hz, us, wide
        self.tunes.append((hz, wide, us))

    def level(self, us):
        p = 10 ** (FLOOR / 10)
        bw = 12_000 if self.wide else 6_000
        for hz, dbm, spans in self.sig:
            d = abs(hz - self.f)
            if d <= 1.5 * bw and any(a <= us < b for a, b in spans):
                p += 10 ** ((dbm - (0 if d <= bw else 20)) / 10)
        return 10 * math.log10(p)

    def rssi(self, us):
        g = ramp(us - self.t, WIDE if self.wide else NARROW)
        return rssi_reg(round(-173 + (self.level(us) + 173) * g + self.rnd.uniform(-1, 1)))

    def noise(self, us):
        return 0 if self.level(us) > -100 else 0x2A


def go(blk, model, keys=(), level=4, spare=None, code=None):
    from modem import k6mod
    if code is None:
        code, entry = k6mod.build(SRC)
    else:
        code, entry = code, 0
    regs = {**START, 0x67: model.rssi, 0x63: model.noise}
    return run(regs, code=(sw.patch(code, blk), entry), tune=model.tune, level=level, keys=keys,
               spare=sw.STORE - len(code) if spare is None else spare, count=3_000_000_000)


def s_(sec):
    return int(sec * 1e6)


@need_clang
def test_builds_and_fits_2m_and_a_70cm_half():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 2 and entry == 0
    assert code.count(sw.MAGIC) == 1
    assert len(sw.pack([sw.SEGMENTS["2m"][:4]])) == 76
    for name in ("2m", "125", "70a", "70b", "70w"):
        assert sw.fits([sw.SEGMENTS[name][:4]], code=len(code)), name
    assert sw.fits([sw.SEGMENTS["2m"][:4], sw.SEGMENTS["125"][:4], (440_000_000, 25_000, 300, 12_500)], code=len(code))
    assert not sw.fits([(144_000_000, 10_000, 1100, 5_000)], code=len(code))
    assert sw.fits([(144_000_000, 10_000, 1000, 5_000)], cols=128, code=len(code))
    assert not sw.fits([(144_000_000, 10_000, 1000, 5_000)], cols=0, code=len(code))
    assert sw.spare(len(code)) >= 32, len(code)          # some room to grow (CI builds ~24 B bigger)
    blk = sw.pack([sw.SEGMENTS["2m"][:4]], seconds=30)
    assert sw.unpack(blk)["segs"] == [sw.SEGMENTS["2m"][:4]] and sw.unpack(blk)["max_s"] == 30
    with pytest.raises(ValueError):
        sw.patch(code + code, blk)


def two_m(seconds=40, **kw):
    return sw.pack([(146_600_000, 10_000, 80, 5_000)], seconds=seconds, **kw)


class Jumpy(Receiver):
    """quick reads (under 3 ms after a tune) on a third of the bins jump 4-9 dB half the time"""

    def rssi(self, us):
        v = super().rssi(us)
        if us - self.t < 3000 and (self.f // 10000) % 3 == 0 and self.rnd.random() < 0.5:
            v += 2 * self.rnd.randint(4, 9)       # the register counts 0.5 dB
        return v


@need_clang
def test_quick_reads_that_jump_are_confirmed_less_and_less():
    # 70 cm on air (2026-11-07) fell to 114 bins/s confirming bins that never made a hit
    ret, said, *_ = go(two_m(20), Jumpy([]))
    res = sw.decode("\n".join(said))
    conf = [s["c"] for s in res["sweeps"]]
    assert ret == 0 and not res["new"]
    assert sum(conf[-20:]) / 20 <= 6, conf
    assert res["z"]["bps"] >= 320, res["z"]


@need_clang
def test_a_list_full_of_weak_carriers_stays_fast_and_takes_a_repeater():
    # 70 cm on air (2026-11-07): weak carriers filled the hot list, every other hit was refined
    # and dropped each sweep, and a sweep took 14 s. Now a hit with no room costs nothing until it
    # is hit_db louder than the weakest entry, which it then replaces.
    weak = [(146_600_000 + k * 40_000, -105, [(s_(5), s_(40))]) for k in range(20)]
    rep = [(146_940_000, -70, [(s_(25), s_(28))])]
    ret, said, *_ = go(two_m(35), Receiver(weak + rep))
    res = sw.decode("\n".join(said))
    assert res["z"]["hot"] == sw.HOT and res["z"]["bps"] >= 300, res["z"]
    slow = [s["ms"] for s in res["sweeps"] if s["ms"] >= 400]     # the sweeps that list new entries
    assert len(slow) <= 3, slow
    o = [o for o in res["overs"] if o["f"] == 146_940_000]
    assert len(o) == 1 and abs(o[0]["lvl"] + 70) <= 2 and 2400 <= o[0]["ms"] <= 3400, o


@need_clang
def test_finds_overs_refines_and_reports():
    # a repeater on 146.940 keyed for 3 s three times at -70, a weak simplex station on 146.715
    # (between coarse bins: 7.5 kHz off 146.71/.72 isn't, but it is 5 kHz off) once at -105, and the
    # upload's own carrier on 440.850 for the first second
    rep = [(s_(4), s_(7)), (s_(12), s_(15)), (s_(22), s_(25))]
    m = Receiver([(146_940_000, -70, rep), (146_715_000, -105, [(s_(9), s_(13))]),
                  (440_850_000, -20, [(0, s_(1))])])
    ret, said, _, writes = go(two_m(32), m)
    assert writes == [] and all(len(ln) <= 60 for ln in said)
    res = sw.decode("\n".join(said))
    assert res["floors"] and abs(res["floors"][0] - FLOOR) <= 2
    news = {e["f"] for e in res["new"]}
    assert 146_940_000 in news and 146_715_000 in news, res["new"]
    overs = [o for o in res["overs"] if o["f"] == 146_940_000]
    assert len(overs) == 3, overs
    for o, start in zip(overs, (4000, 12000, 22000)):
        assert 2600 <= o["ms"] <= 3400 and abs(o["lvl"] + 70) <= 2, o
        assert abs(o["at"] - start) <= 1500, o          # when it started, for the explorer's ID cue
    t = {e["f"]: e for e in res["summary"]}
    assert t[146_940_000]["overs"] == 3 and t[146_940_000]["spread"] <= 2 and not t[146_940_000]["spur"]
    assert t[146_715_000]["overs"] == 1
    assert res["z"]["hot"] == 2 == ret and res["z"]["sweeps"] == len(res["sweeps"])
    # the summary alone, as the telemetry keeps it, decodes the same
    tl = sw.decode("".join(ln + "\n" for ln in said)[-sw.TLM_BYTES:])
    assert tl["summary"] == res["summary"] and tl["z"] == res["z"]
    rep_txt = sw.report(res, [(146_600_000, 10_000, 80, 5_000)])
    assert "146.940" in rep_txt and "bins/s" in rep_txt


@need_clang
def test_a_carrier_that_never_moves_is_a_spur():
    m = Receiver([(146_800_000, -90, [(0, s_(1000))]), (146_940_000, -70, [(s_(5), s_(8))])])
    blk = two_m(30, spur_s=8)
    ret, said, *_ = go(blk, m)
    res = sw.decode("\n".join(said))
    assert [x["f"] for x in res["spurs"]] == [146_800_000]
    t = {e["f"]: e for e in res["summary"]}
    assert t[146_800_000]["spur"] and not t[146_940_000]["spur"]
    assert ret == 1
    # once it's a spur, the revisits stop: in the last 8 s it's tuned once a sweep, as any bin
    late = [hz for hz, _, us in m.tunes if us > s_(22)]
    assert late.count(146_800_000) <= late.count(146_610_000) + 1
    early = [hz for hz, _, us in m.tunes if s_(6) < us < s_(14)]
    assert early.count(146_800_000) > early.count(146_610_000) + 10    # revisited while it was up


@need_clang
def test_rows_show_the_carrier_in_its_column():
    m = Receiver([(146_940_000, -70, [(0, s_(1000))])])
    blk = sw.pack([(146_600_000, 10_000, 80, 5_000)], seconds=6, cols=40, spur_s=600)
    ret, said, *_ = go(blk, m)
    res = sw.decode("\n".join(said))
    rows = [s["row"] for s in res["sweeps"][1:]]
    assert rows and all(len(r) == 40 for r in rows)
    col = (146_940_000 - 146_600_000) // 10_000 * 40 // 80
    for r in rows:
        assert max(range(40), key=lambda i: r[i]) in (col - 1, col), r   # 146.93 is in its skirt
        assert r[col] >= 20 and max(r[:col - 1] + r[col + 2:]) < 8, r   # the carrier, noise elsewhere


@need_clang
def test_cost_of_a_bin_comes_out_of_the_sweep_lines():
    m = Receiver([])
    segs = [(146_600_000, 10_000, 80, 5_000)]
    ret, said, *_ = go(sw.pack(segs, seconds=5), m)
    res = sw.decode("\n".join(said))
    assert ret == 0 and res["summary"] == []
    # the emulator charges 400 us a tune and 20 us a read: what the decoder must find
    assert abs(sw.bin_cost_us(res, segs) - 420) < 40
    assert res["z"]["bps"] == pytest.approx(80e6 / (80 * 1920 + 12400), rel=0.1)   # with the park


@need_clang
def test_two_segments_in_turn():
    m = Receiver([(146_940_000, -70, [(s_(3), s_(9))]), (442_500_000, -80, [(s_(3), s_(9))])])
    segs = [(146_600_000, 10_000, 80, 5_000), (442_000_000, 12_500, 80, 12_500)]
    ret, said, *_ = go(sw.pack(segs, seconds=14), m)
    res = sw.decode("\n".join(said))
    assert {s["seg"] for s in res["sweeps"]} == {0, 1} and len(res["floors"]) == 2
    assert {e["f"] for e in res["summary"]} == {146_940_000, 442_500_000}


@need_clang
def test_key_bad_block_small_scratch_old_image():
    m = Receiver([])
    ret, said, *_ = go(two_m(0), m, keys=[(s_(4), s_(5))])
    assert ret == 0 and said[-1].startswith("Z sw=")
    bad = bytearray(two_m())
    bad[8] = 0                                          # no segments
    assert go(bytes(bad), m)[0] == -11
    bad = bytearray(two_m())
    bad[16:18] = (20000).to_bytes(2, "little")          # quick read after the confirm
    assert go(bytes(bad), m)[0] == -11
    big = sw.pack([(144_000_000, 10_000, 1100, 5_000)])
    assert go(big, m)[0] == -8
    assert go(two_m(), m, spare=sw.SW_T + sw.HOT * sw.HOT_T - 4)[0] == -8    # no room for the state
    assert go(sw.pack([(144_000_000, 10_000, 1000, 5_000)], cols=0), m)[0] == -8   # nor for the row
    assert go(two_m(), m, level=1)[0] == -3


def test_pack_parse_and_decode():
    assert sw.parse_seg("144.0-148.0/10") == (144_000_000, 10_000, 400, 5_000)
    assert sw.parse_seg("440-445/12.5") == (440_000_000, 12_500, 400, 12_500)
    with pytest.raises(ValueError):
        sw.pack([(144_000_000, 10_000, 4, 5_000)])
    log = ("12:00:01 K6 MOD say F -137\n"
           "12:00:01 K6 MOD say R0 0 0005\n"
           "12:00:02 K6 MOD say S0 1 160 c0 h0\n"
           "12:00:02 K6 MOD say N 146.940 -70 fl-137 z0 b34\n"
           "12:00:05 K6 MOD say H 146.940 3010 -70 n1\n"
           "12:00:06 K6 MOD say H 146.940 900 -71 a5120\n"
           "12:00:09 K6 MOD say T 146.940 n1 t3 -70 d0\n"
           "12:00:09 K6 MOD say Z sw=20 bins=1600 ms=8000 bps=200 hot=1\n")
    res = sw.decode(log)
    assert res["sweeps"][0]["row"] == [0, 0, 0, 5] and res["sweeps"][0]["t"] == "12:00:02"
    assert res["overs"][0] == {"f": 146_940_000, "ms": 3010, "lvl": -70, "n": 1, "t": "12:00:05"}
    assert res["overs"][1] == {"f": 146_940_000, "ms": 900, "lvl": -71, "at": 5120, "t": "12:00:06"}
    assert res["z"]["bps"] == 200 and res["summary"][0]["overs"] == 1


def test_watch_shows_a_row_a_sweep():
    shown, pending = [], []
    for ln in ["F -137", "R0 0 00", "R0 2 9o", "N 146.940 -70 fl-137 z0 b34", "S0 3 160 c1 h1",
               "Z sw=4 bins=320 ms=700 bps=457 hot=1"]:
        sw.watch_line(ln, pending, shown.append)
    assert shown[0] == "  floors -137" and shown[1].startswith("  new  146.940")
    res = sw.decode("K6 MOD say F0 -120 q-160: -121 -120 -93 -119 -118 -122 -120 -117\n"
                    "K6 MOD say F1 -137 q-161: -137 -138 -136 -135 -137 -139 -140 -137")
    assert res["floors"] == [-120, -137] and res["quick_floors"] == {0: -160, 1: -161}
    assert res["floor_reads"][0][2] == -93
    assert shown[2] == "0 |  :@|   160 ms c1" and shown[-1].startswith("Z ") and pending == []
