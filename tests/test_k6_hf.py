"""The HF probe module (c/k6/mod/hfprobe.c) on an emulated Cortex-M0, with its parameter block
patched the way tools/k6_hf.py patches CI's .bin: each probe and its floor 30 kHz above, per
configuration; the front-end path pins (PA13/PA14) set after every tune and released at the end;
a configuration's register written and put back; the S rows (the telemetry) decode to the same
numbers as the serial p lines. SPDX-License-Identifier: MIT"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, rssi_reg, run  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_hf as hf  # noqa: E402


class Rx:
    """a receiver whose synthesizer stops at lo_hz (below it, it sits at lo_hz), with a signal at each
    of sig {hz: dBm}; the front end loses 20 dB with neither path on, and REG_3E != 0xA037 loses lock"""

    def __init__(self, sig, lo_hz=14_500_000):
        self.sig, self.lo, self.f, self.pins, self.regs = sig, lo_hz, 440_850_000, 0, None

    def tune(self, hz, wide, us):
        self.f = max(hz, self.lo)
        self.pins = 1 << 14 if hz < 300e6 else 1 << 13         # Aura's choice

    def gpio_w(self, uc, off, size, value, st):
        assert off in (0x18, 0x28), f"GPIOA+{off:#x}"
        self.regs = st["regs"]                                  # run() works on its own copy
        assert not value & ~((1 << 13) | (1 << 14)), f"touched other pins {value:#x}"
        self.pins = self.pins | value if off == 0x18 else self.pins & ~value

    def rssi(self, us):
        lvl = -130
        if self.regs.get(0x3E, 0xA037) == 0xA037:
            lvl = max([lvl] + [d for f, d in self.sig.items() if abs(f - self.f) < 6_000])
        if not self.pins:
            lvl -= 20
        return rssi_reg(lvl)


def go(blk, rx, keys=()):
    from modem import k6mod
    code, entry = k6mod.build(hf.SRC)
    regs = {**START, 0x3E: 0xA037}
    rx.regs = regs
    regs[0x67] = rx.rssi
    mmio = [(0x48000000, 0x400, lambda uc, off, size, st: 0, rx.gpio_w)]
    return run(regs, code=(hf.patch(code, blk), entry), tune=rx.tune, mmio=mmio, keys=keys, level=4,
               spare=1024, count=500_000_000)


@need_clang
def test_builds_small_level_2_one_block():
    from modem import k6mod
    code, entry = k6mod.build(hf.SRC)
    assert k6mod.source_level(hf.SRC) == 2 and entry == 0
    assert code.count(hf.MAGIC) == 1 and len(code) < 2048
    assert len(hf.pack([1], [(0, 0, 0)])) == 116


@need_clang
def test_finds_where_the_synthesizer_stops():
    sig = {26_000_000: -100, 13_000_000: -95, 15_000_000: -105, 10_000_000: -90}
    rx = Rx(sig)
    probes = [26_000_000, 15_000_000, 13_000_000, 10_000_000]
    cfgs = [(0, 0, 0), (3, 0, 0), (0, 0x3E, 0xC000)]
    ret, said, regs, writes = go(hf.pack(probes, cfgs), rx)
    res = hf.decode("\n".join(said))
    # above the stop: heard; below it the synthesizer sits at 14.5 MHz and hears nothing
    assert res["p"][(0, 26_000_000)] == (-100, -130) and res["p"][(0, 15_000_000)] == (-105, -130)
    assert res["p"][(0, 13_000_000)] == (-130, -130) and res["p"][(0, 10_000_000)] == (-130, -130)
    assert res["p"][(1, 26_000_000)] == (-120, -150)          # no front-end path: 20 dB down
    assert res["p"][(2, 26_000_000)] == (-130, -130)          # the register broke it ...
    assert regs[0x3E] == 0xA037 and (0x3E, 0xC000) in writes   # ... and is put back
    assert res["r"][2] == "REG_3E a037>c000 ok"
    assert ret == 4                                            # 2 heard in each of c0 and c1
    for c in range(3):                                         # telemetry rows = serial lines (2 dB)
        for k, f in enumerate(probes):
            s, r = res["S"][c][k]
            ps, pr = res["p"][(c, f)]
            assert abs(s - ps) <= 1 and abs(r - pr) <= 1
    assert rx.pins == 1 << 14                                  # path 0 at the end: Aura's own
    assert sum(len(x) + 1 for x in said[-4:]) < 12 * 57


@need_clang
def test_a_key_stops_it_and_restores():
    rx = Rx({})
    ret, said, regs, writes = go(hf.pack([20_000_000] * 8, [(0, 0x3E, 0x8000), (3, 0, 0)]), rx,
                                 keys=((50_000, 10**9),))
    assert ret == -2 and regs[0x3E] == 0xA037


def test_cfg_parsing():
    assert hf.parse_cfg("0:3e=b000") == (0, 0x3E, 0xB000) and hf.parse_cfg("4") == (4, 0, 0)
    for bad in ("5", "0:30=1", "0:37=0"):
        try:
            hf.parse_cfg(bad)
        except SystemExit:
            continue
        raise AssertionError(bad)


class Bands:
    """a chip that picks its divider like the guide's table (VCO 1.48-2.24 GHz, N up to 96), shows the
    band code in REG_1A, locks (REG_0C bit 9) only where some N fits, and has a noisy RSSI"""
    NS = [(2, 0x1F80), (4, 0x2F50), (6, 0x9F30), (8, 0x3F48), (12, 0xAF28), (16, 0x4F44), (24, 0xBF24),
          (32, 0x5F42), (48, 0xCF22), (64, 0x6F41), (96, 0xDF21)]

    def __init__(self):
        self.f, self.n = 440_850_000, 0

    def tune(self, hz, wide, us):
        self.f = hz

    def band(self):
        for n, code in self.NS:
            if 1.48e9 <= self.f * n <= 2.24e9:
                return code, True
        return 0xDF21, False

    def reg(self, r):
        def f(us):
            self.n += 1
            if r == 0x1A:
                return self.band()[0]
            if r == 0x0C:
                return 0x0200 if self.band()[1] else 0
            if r == 0x67:
                return rssi_reg(-130 + self.n % 3)            # changes between reads: not reported
            if r == 0x5F:
                raise AssertionError("read the FSK FIFO")
            return 0x1234
        return f


@need_clang
def test_bandreg_reports_the_band_register_and_lock():
    from modem import k6mod
    code, entry = k6mod.build(hf.BAND_SRC)
    assert k6mod.source_level(hf.BAND_SRC) == 2 and len(code) < 1200 and code.count(hf.BAND_MAGIC) == 1
    chip = Bands()
    regs = {**START, **{r: chip.reg(r) for r in range(128) if r != 0x5F}}
    hz = [440_850_000, 145_000_000, 18_000_000, 16_000_000, 14_000_000]
    ret, said, regs_end, writes = run(regs, code=(hf.band_patch(code, hf.band_pack(hz)), entry), tune=chip.tune,
                                      level=4, spare=1024, count=500_000_000)
    assert writes == [] and ret == 4
    rows = hf.band_decode("\n".join(said))
    assert [r[0] for r in rows] == hz
    assert rows[0][2] == {} and rows[0][1] == 0x0200
    assert rows[1][2] == {0x1A: 0xAF28}                         # only the band register moved, not RSSI
    assert rows[3][2] == {0x1A: 0xDF21} and rows[3][1] & 0x200
    assert rows[4][1] == 0 and rows[4][2] == {0x1A: 0xDF21}     # 14 MHz: no divider fits, no lock
    assert "NO" in hf.band_table(rows).splitlines()[-1]
