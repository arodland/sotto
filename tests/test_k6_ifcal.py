"""The calibration module (c/k6/mod/ifcal.c) on an emulated Cortex-M0 against gp0dma's modelled
DMA and GP0 IF: with the AFC off it measures where a bare carrier lands on the IF, writes the
calibration record (c/k6/mod/k6_cal.h) through level 4's cal_write, and refuses to write when
the carrier wanders. The record reads back with the same helpers the IF-tap modules use.
SPDX-License-Identifier: MIT"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, dma_bench, need_clang, rssi_reg, run  # noqa: E402

SRC = os.path.join(ROOT, "c", "k6", "mod", "ifcal.c")
NOMINAL = 26e6 / 1536


def small(tmp_path, extra=""):
    """ifcal at its own block length (BLK 940 reads the IF a couple of Hz out in the model)"""
    w = tmp_path / "ifcal_small.c"
    w.write_text(f'{extra}#include "{SRC}"\n')
    return str(w)


def record(b):
    """k6_cal.h's 32 bytes -> dict, with the check verified"""
    magic, ver, flags, off, ppb, hz, blocks, spread, rssi, _, check, _ = struct.unpack("<4sHHiiIHHhHHH", b[:32])
    words = struct.unpack("<14H", b[:28])
    assert check == (0x6B36 + sum(words)) & 0xFFFF
    return dict(magic=magic, ver=ver, flags=flags, off=off, ppb=ppb, hz=hz, blocks=blocks, spread=spread,
                rssi=rssi)


def if_of(offset_hz, wander=0.0):
    """the carrier's IF: nominal + offset with the AFC off (REG_73 bit 4), pulled to nominal with
    it on (the chip's AFC); wander: Hz per second of drift"""
    def f(t, regs):
        if regs.get(0x73, 0) & 0x10:
            return NOMINAL + offset_hz + wander * t
        return NOMINAL + 0 * t
    return f


@need_clang
def test_builds_level_4_with_room_for_the_ring():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 4 and entry == 0
    assert 4096 - 24 - len(code) >= 516, len(code)


@need_clang
def test_measures_and_writes_the_offset(tmp_path):
    mmio, rcc, tim, dma, sim = dma_bench(f_if=if_of(-145.5), dev=0.0)
    regs = {**START, 0x73: 0x4682, 0x43: 0x3028,
            0x67: lambda us: rssi_reg(-41 if us >= 3_000_000 else -118)}
    cal = {}
    ret, said, regs, writes = run(regs, src=small(tmp_path), spare=800, mmio=mmio, level=4, cal=cal)
    assert said[1] == "ifcal start floor=-118 rx=440850000 old=none", said
    assert len(cal["writes"]) == 1, said
    rec = record(cal["flash"])
    assert rec["magic"] == b"K6CL" and rec["ver"] == 1 and rec["flags"] == 1
    assert abs(rec["off"] / 100 + 145.5) < 1.0, (rec, said)
    assert abs(rec["ppb"] - rec["off"] / 100 / 440.85e6 * 1e9) < 2, rec
    assert rec["hz"] == 440_850_000 and rec["blocks"] == 32 and rec["rssi"] == -41
    assert abs(ret + 1455) < 10, ret
    # every block listed: the first in Hz, then steps; they rebuild the mean it measured
    vals = []
    for ln in (ln for ln in said if ln.startswith("ifcal b")):
        for w in ln.split()[2:]:
            vals.append(int(w) + (vals[-1] if vals else 0))
    assert len(vals) == 32 and abs(sum(vals) / 32 - (NOMINAL - 145.5)) < 1.0, vals
    off = next(ln for ln in said if ln.startswith("ifcal off="))
    assert off.startswith("ifcal off=-14") and off.endswith(" wrote"), said
    # put back: AFC as it was, GP0, clocks, the DMA channel
    assert regs[0x73] == 0x4682 and regs[0x33] == 0xd300 and regs[0x34] == 0
    assert rcc[0x18] == 0 and dma[0x08] == 0
    assert all(len(ln) <= 60 for ln in said)
    # a second run sees the old record
    mmio, *_ = dma_bench(f_if=if_of(-145.5), dev=0.0)
    regs = {**START, 0x73: 0x4682, 0x43: 0x3028, 0x67: lambda us: rssi_reg(-41 if us >= 3_000_000 else -118)}
    cal2 = {"flash": cal["flash"]}
    ret, said, *_ = run(regs, src=small(tmp_path, "#define IFCAL_DRY 1\n"), spare=800, mmio=mmio, level=4,
                        cal=cal2)
    assert said[1] == f"ifcal start floor=-118 rx=440850000 old={rec['off']}", said
    assert next(ln for ln in said if ln.startswith("ifcal off=")).endswith("dry run") and "writes" not in cal2


@need_clang
def test_refuses_a_wandering_carrier_and_old_images(tmp_path):
    mmio, *_ = dma_bench(f_if=if_of(-145.5, wander=3000.0), dev=0.0)
    regs = {**START, 0x73: 0x4682, 0x67: lambda us: rssi_reg(-41 if us >= 3_000_000 else -118)}
    cal = {}
    ret, said, *_ = run(regs, src=small(tmp_path), spare=800, mmio=mmio, level=4, cal=cal)
    assert ret == -9 and "writes" not in cal and any("refused" in ln for ln in said), said
    ret, *_ = run(START, src=small(tmp_path), spare=800, level=3)
    assert ret == -3


@pytest.mark.parametrize("hz", [440_850_000, 430_000_000, 449_975_000, 223_500_000, 146_520_000])
@pytest.mark.parametrize("ppb", [-376, -330, 120, 4500, -4500])
def test_offset_scales_with_frequency(hz, ppb):
    """k6cal_off_chz's integer scaling, as k6_cal.h does it: within 0.15% (+ 1 for the floors)
    of ppb x hz, and inside 32 bits all the way"""
    v = (ppb * (hz >> 14)) >> 6
    assert abs(v * 215) < 2 ** 31
    got = (v * 215) >> 11
    want = ppb * 1e-9 * hz * 100
    assert abs(got - want) <= abs(want) * 0.0015 + 1.5, (got, want)


@need_clang
@pytest.mark.parametrize("junk", [b"\x00" * 32, b"\xff" * 31 + b"\x7f", b"SSTV" + b"\xff" * 28])
def test_leaves_a_sector_that_is_not_ours(tmp_path, junk):
    """the calibration sector holds neither 0xFF nor a K6CL record: something else uses it, so
    ifcal returns -11 before it measures, and never calls cal_write (which erases the sector)"""
    mmio, *_ = dma_bench(f_if=if_of(-145.5), dev=0.0)
    regs = {**START, 0x73: 0x4682, 0x43: 0x3028,
            0x67: lambda us: rssi_reg(-41 if us >= 3_000_000 else -118)}
    cal = {"flash": junk + b"\xff" * 32}
    ret, said, regs2, writes = run(regs, src=small(tmp_path), spare=800, mmio=mmio, level=4, cal=cal)
    assert ret == -11 and "writes" not in cal and cal["flash"][:32] == junk, said
    assert not writes, writes                       # no chip register touched either


@need_clang
def test_overwrites_a_damaged_record_of_its_own(tmp_path):
    """a K6CL record with a bad check word is ours: ifcal measures and writes a good one over it"""
    mmio, *_ = dma_bench(f_if=if_of(-145.5), dev=0.0)
    regs = {**START, 0x73: 0x4682, 0x43: 0x3028,
            0x67: lambda us: rssi_reg(-41 if us >= 3_000_000 else -118)}
    bad = b"K6CL" + b"\x01\x00\x01\x00" + b"\x00" * 24
    cal = {"flash": bad + b"\xff" * 32}
    ret, said, *_ = run(regs, src=small(tmp_path), spare=800, mmio=mmio, level=4, cal=cal)
    assert said[1].endswith("old=none") and len(cal["writes"]) == 1, said
    assert record(cal["flash"])["magic"] == b"K6CL" and abs(ret + 1455) < 10
