"""The crystal-step module (c/k6/mod/xtalstep.c) on an emulated Cortex-M0, against the GP0/DMA
bench of test_k6_i2s with an IF that follows REG_3B (a live crystal register, with a first-order
synthesizer lag) or ignores it (a register only taken at RXON)."""
import os
import sys

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, dma_bench, need_clang, rssi_reg, run  # noqa: E402

SRC = os.path.join(ROOT, "c", "k6", "mod", "xtalstep.c")
BASE3B = 0x5880
HZ_PER_STEP = -84.8      # the IF's move per REG_3B step (the sign is the K6's to tell)
TAU = 60e-6              # the modelled synthesizer's time constant
FS = 5.71e6


def small(tmp_path):
    w = tmp_path / "xtalstep_small.c"
    w.write_text(f'#define NA 768u\n#define SKIP 128u\n#define HALF 96u\n#define WRITES 60u\n'
                 f'#include "{SRC}"\n')
    return str(w)


def lagged_if(live):
    st = {"y": 0.0}

    def f_if(t, regs):
        k = (regs.get(0x3B, BASE3B) - BASE3B) if live else 0
        k = k - 0x10000 if k > 0x7FFF else k
        # one step of the lag per sample, from the value at the end of the last block
        a = np.exp(-1 / (FS * TAU))
        y = k + (st["y"] - k) * a ** np.arange(1, len(t) + 1)
        st["y"] = y[-1]
        return 17000.0 + HZ_PER_STEP * y
    return f_if


def chip():
    return {**START, 0x3B: BASE3B, 0x3C: 0x4F00, 0x38: 0x7a12, 0x39: 0x02a0, 0x73: 0x4682,
            0x43: 0x3028, 0x67: lambda us: rssi_reg(-21 if us >= 3_000_000 else -103)}


def lines(said, tag):
    out = []
    for ln in said:
        if ln.startswith(tag):
            f = ln.split()
            out.append((int(f[2]), dict(kv.split("=") for kv in f[3:])))
    return out


@need_clang
def test_builds_fits_level_2():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 2
    # the 4 KB store holds the code, the 24-byte header and tag, the 1 KB ring and the bins
    assert 24 + len(code) + 4 + 1024 + 220 + 8 <= 4096, len(code)


@need_clang
def test_live_register_moves_the_if_and_shows_the_lag(tmp_path):
    mmio, rcc, tim, dma, sim = dma_bench(f_if=lagged_if(True), dev=0.0)
    ret, said, regs, writes = run(chip(), src=small(tmp_path), spare=1500, mmio=mmio)
    assert said[0] == "xs regs 3b=5880 3c=4f00 38=7a12 39=02a0 73=4682 43=3028", said[0]
    a = lines(said, "xs a ")
    assert [k for k, _ in a] == [0, 6, -6, 0, 24, -24, 0, 48, -48, 0]
    for k, x in a:
        assert abs(int(x["if"]) - (17000 + HZ_PER_STEP * k)) < 40, (k, x)
        assert 5650 <= int(x["ks"]) <= 5770 and "lost" not in x, x
    assert abs(ret - HZ_PER_STEP * 24) < 40, ret
    b = lines(said, "xs b ")
    assert [k for k, _ in b] == [100, -100, 0] and all(abs(int(x["if"]) - 17000) < 40 for _, x in b)
    assert not lines(said, "xs l ")
    # C: the up steps go from base - 24 to base + 24 (the IF falls by 48 steps' worth) with
    # the model's lag; the first bin starts as the write starts
    up = [int(v) for ln in said if ln.startswith("c up ") for v in ln.split(":")[1].split()]
    dn = [int(v) for ln in said if ln.startswith("c dn ") for v in ln.split(":")[1].split()]
    assert len(up) == len(dn) == 16
    lo, hi = 17000 + HZ_PER_STEP * 24, 17000 - HZ_PER_STEP * 24
    assert abs(up[-1] - lo) < 80 and abs(dn[-1] - hi) < 80, (up, dn)
    assert up[0] > up[-1] + 1000 and dn[0] < dn[-1] - 1000, (up, dn)   # not there at once
    c = [ln for ln in said if ln.startswith("xs c wr=")]
    assert len(c) == 1 and " n=60 " in c[0] and " lost=0 " in c[0], c
    assert said[-1] == "xs done"
    # all put back
    assert regs[0x3B] == BASE3B and regs[0x38] == 0x7a12 and regs[0x73] == 0x4682
    assert regs[0x33] == 0xd300 and regs[0x34] == 0
    assert rcc[0x18] == 0 and rcc[0x14] == 0x14 and dma[0x08] == 0 and tim[0x00] == 0
    assert {r for r, _ in writes} <= {0x3B, 0x38, 0x73, 0x33, 0x34}
    assert all(len(ln) <= 60 for ln in said)


@need_clang
def test_latched_register_takes_the_rxon_branch(tmp_path):
    mmio, rcc, tim, dma, sim = dma_bench(f_if=lagged_if(False), dev=0.0)
    ret, said, regs, writes = run(chip(), src=small(tmp_path), spare=1500, mmio=mmio)
    assert abs(ret) < 40
    assert [k for k, _ in lines(said, "xs l ")] == [24, 0]
    assert not [ln for ln in said if ln.startswith("c ")]
    assert regs[0x3B] == BASE3B and regs[0x73] == 0x4682 and said[-1] == "xs done"


@need_clang
def test_refuses_without_scratch(tmp_path):
    mmio, *_ = dma_bench()
    ret, said, regs, writes = run(chip(), src=small(tmp_path), spare=1200, mmio=mmio)
    assert ret == -8 and not writes
