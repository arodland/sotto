"""The receive coverage module (c/k6/mod/rxmap.c) on an emulated Cortex-M0 against a modelled
FD6818 receiver, with its parameter block patched the way tools/k6_rxmap.py patches CI's .bin:

  - map: the quick pass ranks each bin's channels, the full reads give the bin's floor and peak,
    the summary (T, F, P, M lines) fits the over-air telemetry and decodes to the same bins as the
    serial detail lines;
  - cal: each IC-7100 carrier becomes one line, in order, at its level over its point's floor;
  - a key stops it; a bad block is refused.

The receiver model: RSSI restarts at -173 dBm on every tune and ramps to the true level as the
explorer's Phase 0 measured on the radio (wide: 59 % of the way in dB at 3 ms, all of it at 12 ms).
The true level is the noise floor plus the signals within 12 kHz, cut 30 dB outside the K6's
nominal filters (136-174, 400-520 MHz). SPDX-License-Identifier: MIT"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, rssi_reg, run  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_rxmap as rx  # noqa: E402

SRC = rx.SRC
RAMP = [(0, 0.0), (1000, 0.22), (1500, 0.36), (2000, 0.46), (3000, 0.59), (5000, 0.70), (8000, 0.91),
        (12000, 1.0)]
FLOOR = -137


def ramp(us):
    for (t0, g0), (t1, g1) in zip(RAMP, RAMP[1:]):
        if us < t1:
            return g0 + (g1 - g0) * (us - t0) / (t1 - t0)
    return 1.0


def in_filter(hz):
    return 136e6 <= hz <= 174e6 or 400e6 <= hz <= 520e6


class Receiver:
    """signals: [(hz, dBm, from_us, to_us)]; the K6 sees them within 12 kHz of its tuning"""

    def __init__(self, signals):
        self.sig, self.f, self.t = signals, 440_850_000, 0

    def tune(self, hz, wide, us):
        self.f, self.t = hz, us

    def level(self, us):
        p = 10 ** (FLOOR / 10)
        for hz, dbm, a, b in self.sig:
            if abs(hz - self.f) <= 12_000 and a <= us < b:
                p += 10 ** ((dbm - (0 if in_filter(hz) else 30)) / 10)
        import math
        return 10 * math.log10(p)

    def rssi(self, us):
        g = ramp(us - self.t)
        return rssi_reg(round(-173 + (self.level(us) + 173) * g))

    def noise(self, us):
        return 0 if self.level(us) > -95 else 0x2A


def go(blk, model, keys=(), level=4):
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    regs = {**START, 0x67: model.rssi, 0x63: model.noise}
    return run(regs, code=(rx.patch(code, blk), entry), tune=model.tune, level=level, keys=keys,
               spare=4072 - len(code), count=2_000_000_000)


@need_clang
def test_builds_small_with_one_block_and_room():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 2 and entry == 0
    assert code.count(rx.MAGIC) == 1 and len(code) < 3000
    assert 4072 - len(code) >= 3 * 340            # the most bins pack() allows fit the scratch RAM
    blk = rx.seg_block("A")
    assert len(blk) == 100 and rx.unpack(blk)["nsteps"] == 80
    assert rx.patch(code, blk).find(blk) == code.find(rx.MAGIC)
    with pytest.raises(ValueError):
        rx.patch(code + code, blk)
    with pytest.raises(ValueError):
        rx.pack(18_000_000, 2_000_000, 160, 15_000)   # not a whole number of steps a bin


@need_clang
def test_map_finds_the_signals_and_the_summary_fits():
    # NOAA in the VHF filter, a pager carrier just above it (30 dB down), a weak one near the floor,
    # and the upload's own carrier on 440.850 for the first 1.5 s (the module waits for it)
    m = Receiver([(162_550_000, -75, 0, 10**12), (176_400_000, -60, 0, 10**12),
                  (158_125_000, -122, 0, 10**12), (440_850_000, -21, 0, 1_500_000)])
    blk = rx.pack(150_000_000, 1_000_000, 30, 25_000)
    ret, said, _, writes = go(blk, m)
    assert writes == [] and all(len(ln) <= 60 for ln in said)
    assert said[0].startswith("rxmap: waiting for the carrier to drop, -2")
    runs = rx.decode("\n".join(said))
    assert len(runs) == 1 and runs[0]["n"] == 30 and runs[0]["lo"] == 150_000_000
    bins = {b["f"]: b for b in runs[0]["bins"]}
    assert bins[162_000_000]["pk"] in (-75, -76) and bins[162_000_000]["at"] == 162_550_000
    assert bins[176_000_000]["pk"] in (-90, -91) and bins[176_000_000]["at"] == 176_400_000
    assert bins[158_000_000]["pk"] - bins[158_000_000]["fl"] >= 10           # weak, still found
    assert all(abs(b["fl"] - FLOOR) <= 1 for b in runs[0]["bins"])
    quiet = [b for f, b in bins.items() if f not in (162e6, 176e6, 158e6)]
    assert all(b["pk"] - b["fl"] < 3 for b in quiet)
    assert ret == 3 and runs[0]["sig"] == 3
    # the summary alone (as the telemetry ring keeps it: the newest 684 bytes) decodes the same
    text = "".join(ln + "\n" for ln in said)[-rx.TLM_BYTES:]
    tl = rx.decode(text)[0]
    assert tl["tops"][:3] == [(162_550_000, bins[162_000_000]["pk"]), (176_400_000, bins[176_000_000]["pk"]),
                              (158_125_000, bins[158_000_000]["pk"])]
    for a, b in zip(tl["bins"], runs[0]["bins"]):
        assert abs(a["fl"] - b["fl"]) <= 2 and abs(a["pk"] - b["pk"]) <= 2, (a, b)
    summary = [ln for ln in said if ln[:1] in "TFPM" and not ln.startswith("rxmap")]
    assert sum(len(ln) + 1 for ln in summary) <= rx.tlm_size(30)
    # the run time the plan quotes is the emulator's within a third
    est = rx.estimate(30, 40, tune_us=400) * 1000      # the emulator charges 400 us a tune
    assert abs(runs[0]["ms"] - est) < est / 3, (runs[0]["ms"], est)
    rep = rx.report(runs)
    assert "162.550 MHz   -75" in rep or "162.550 MHz   -76" in rep
    assert "NOAA weather" in rep


@need_clang
def test_map_of_160_bins_fits_the_telemetry():
    m = Receiver([(f, -50, 0, 10**12) for f in range(100_000_000, 400_000_000, 7_000_000)])
    blk = rx.pack(18_000_000, 2_000_000, 160, 500_000)      # few steps: the emulator's time
    ret, said, *_ = go(blk, m)
    summary = "".join(ln + "\n" for ln in said if ln[:2] in ("T ", "M ") or ln[:1] in "FP" and ln[1:2].isdigit())
    assert len(summary) <= rx.tlm_size(160) <= rx.TLM_BYTES
    tl = rx.decode("".join(ln + "\n" for ln in said)[-rx.TLM_BYTES:])[0]
    assert all(b["fl"] is not None and b["pk"] is not None for b in tl["bins"])
    assert len(tl["tops"]) == 12 and ret > 12


@need_clang
def test_cal_lines_follow_the_carriers():
    pts = [144_050_000, 146_550_000, 440_850_000]
    sched = rx.cal_schedule(pts[:2], [1.0, 0.1])
    sigs, t = [], 3_000_000
    levels = {(144_050_000, None): -70, (146_550_000, None): -82, (440_850_000, 1.0): -60,
              (440_850_000, 0.1): -70}
    for hz, pw in sched:
        sigs.append((hz, levels[(hz, pw)], t, t + 4_000_000))
        t += 6_000_000
    m = Receiver(sigs)
    blk = rx.pack(440_850_000, 0, 0, 0, mode=1, cal=pts, quiet_s=6, max_s=120)
    ret, said, _, writes = go(blk, m)
    runs = rx.decode("\n".join(said))
    assert len(runs) == 1 and runs[0]["done"] and writes == []
    assert runs[0]["floors"] == {f: FLOOR for f in pts}
    ev = runs[0]["events"]
    assert [e["f"] for e in ev] == [hz for hz, _ in sched] and ret == len(sched)
    for e, (hz, pw) in zip(ev, sched):
        assert abs(e["mean"] - levels[(hz, pw)]) <= 1 and e["fl"] == FLOOR and e["z"] == 0, e
        assert 35 <= e["n"] <= 41, e                  # 4 s at ~0.1 s a cycle of 3 points
    assert "+67" in rx.report(runs)


@need_clang
def test_cal_without_a_carrier_and_a_key_and_bad_blocks():
    blk = rx.pack(440_850_000, 0, 0, 0, mode=1, cal=[146_550_000], quiet_s=5, max_s=4)
    ret, said, *_ = go(blk, Receiver([]))
    assert ret == -6 and said[-1].startswith("K ev=0")
    ret, *_ = go(rx.seg_block("A"), Receiver([]), keys=[(2_000_000, 3_000_000)])
    assert ret == -2
    bad = bytearray(rx.seg_block("A"))
    bad[28] = 7                                       # no such mode
    ret, *_ = go(bytes(bad), Receiver([]))
    assert ret == -11
    ret, *_ = go(rx.seg_block("A"), Receiver([]), level=1)
    assert ret == -3


def test_schedule_and_band_rules():
    assert rx.allowed(144_050_000) and rx.allowed(449_900_000) and rx.allowed(430_100_000)
    assert not rx.allowed(425_000_000)                # north of Line A
    assert rx.allowed(425_000_000, line_a_north=False)
    assert not rx.allowed(144_000_000) and not rx.allowed(148_000_000) and not rx.allowed(121_500_000)
    assert all(rx.allowed(f) for f in rx.CAL_POINTS)
    assert all(rx.allowed(f, hf=True) for f in rx.HF_POINTS) and not any(map(rx.allowed, rx.HF_POINTS))
    assert not rx.allowed(15_000_000, hf=True) and not rx.allowed(14_345_000, hf=True)  # WWV, band edge
    s = rx.cal_schedule([144_050_000], [1.0, 0.5])
    assert s == [(144_050_000, None), (440_850_000, 1.0), (440_850_000, 0.5)]
    assert all(rx.tlm_size(n) <= rx.TLM_BYTES for _, _, n, _, _ in rx.SEGMENTS.values())


def test_decode_serial_log_and_birdies(tmp_path):
    log = ("12:00:01 K6 MOD say b 156.000 -131 -45 @156.000 z0000\n"
           "12:00:01 K6 MOD say b 158.000 -132 -132 @158.400 z002a\n"
           "12:00:02 K6 MOD say M lo=156.000 b=2000 n=2 s=25000 q=3000 sig=1 ms=900\n")
    runs = rx.decode(log)
    assert runs[0]["bins"][0] == {"f": 156_000_000, "fl": -131, "pk": -45, "at": 156_000_000}
    assert rx.birdie(156_000_000) and rx.birdie(144_000_000) and not rx.birdie(162_550_000)
    assert "*" in rx.report(runs)
    p = tmp_path / "x.svg"
    assert rx.svg(runs, str(p)) and p.read_text().startswith("<svg")
