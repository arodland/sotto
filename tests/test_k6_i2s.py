"""The I2S probe module (c/k6/mod/i2sprobe.c) on an emulated Cortex-M0, against a modelled
FD6818 whose GP0 drives the MCU's PB1."""
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "c", "k6", "mod", "i2sprobe.c")
CAP = os.path.join(ROOT, "c", "k6", "mod", "i2scap.c")
TONE = os.path.join(ROOT, "c", "k6", "mod", "gp0tone.c")
sys.path.insert(0, ROOT)
from modem import k6mod  # noqa: E402

need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")
NAMES = ["say", "ticks", "fd_read", "fd_write", "delay_us", "tune", "rx_hz", "ms", "key_down",
         "scratch"]
REFUSED = (0x30, 0x33, 0x36, 0x37)
PERIOD = 400            # samples between modelled DFS edges


def run(regs, moder=0, i2s_en=(0x31, 1), gp0_gate=True, src=SRC, model=None, spare=1024,
        mmio=()):
    """regs: the chip's registers at the start. GP0 drives PB1 only while REG_33 bit 8 is clear
    (else PB1's pull-up reads 1), following REG_34[3:0]: 13 (DFS) toggles every PERIOD reads of
    PB1 once i2s_en's bit is set, 15 (XTAL/2) on every read, others hold 0. gp0_gate: the image
    lets a module change REG_33's GP0 bits (8 and 0); False = an image that refuses REG_33.
    model(sel, reads, regs) -> 0/1 replaces that GP0 model while GP0 drives. spare: the scratch
    RAM the module is given. mmio: (base, size, read(uc, off, size, st), write(uc, off, size,
    value, st)) for more MCU peripherals. Returns (ret, said lines, registers at the end, writes)."""
    unicorn = pytest.importorskip("unicorn")
    from unicorn import arm_const as A
    st = {"us": 0, "said": [], "writes": [], "reads": 0, "pin": 0}
    regs = dict(regs)
    mu = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB | unicorn.UC_MODE_MCLASS)
    mu.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M0)
    ram = 0x20000000
    mu.mem_map(ram, 1 << 20)
    base, api, stubs, stop, sp0 = ram + 0x1000, ram + 0x8000, ram + 0x9000, ram + 0x9F00, ram + 0x7F00
    code, entry = k6mod.build(src)
    mu.mem_write(base, code)
    for i in range(len(NAMES)):
        mu.mem_write(stubs + 16 * i, b"\x70\x47")
    mu.mem_write(stop, b"\x00\xbf" * 8)
    mu.mem_write(api, (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
                 + b"".join((stubs + 16 * i | 1).to_bytes(4, "little") for i in range(len(NAMES))))

    def gpio_read(uc, off, size, _):
        if off == 0x00:
            return moder
        if off == 0x10:
            st["reads"] += 1
            sel = regs.get(0x34, 0) & 15
            if regs.get(0x33, 0) & 0x0100:
                st["pin"] = 1
            elif model:
                st["pin"] = model(sel, st["reads"], regs, st["us"])
            elif sel == 15:
                st["pin"] ^= 1
            elif sel == 13 and regs.get(i2s_en[0], 0) & i2s_en[1]:
                st["pin"] = (st["reads"] // PERIOD) & 1
            else:
                st["pin"] = 0
            return 0x10 | (st["pin"] << 1)          # PB4 high (a keypad row), PB1 = GP0
        return 0

    def gpio_write(uc, off, size, value, _):
        raise AssertionError(f"module wrote GPIOB+{off:#x}")
    mu.mmio_map(0x48000400, 0x400, gpio_read, None, gpio_write, None)
    st["regs"] = regs
    for b, n, r, w in mmio:
        mu.mmio_map(b, n, r, st, w, st)

    def on_stub(uc, addr, size, _):
        name = NAMES[(addr - stubs) // 16]
        r0, r1 = uc.reg_read(A.UC_ARM_REG_R0), uc.reg_read(A.UC_ARM_REG_R1)
        ret = 0
        if name == "say":
            st["said"].append(bytes(uc.mem_read(r0, 64)).split(b"\0")[0].decode())
            assert len(st["said"][-1]) <= 60, st["said"][-1]
            st["us"] += 5000
        elif name == "ticks":
            st["us"] += 4000                       # 25 calls (6400 samples) per 100 ms window
            ret = (st["us"] // 100) & 0xFFFF
        elif name == "fd_read":
            assert r0 != 0x5F, "read the FSK FIFO"
            st["us"] += 20
            ret = regs.get(r0, 0)
            ret = ret(st["us"]) if callable(ret) else ret
        elif name == "fd_write":
            st["writes"].append((r0, r1))
            if r0 == 0x33 and gp0_gate and not (regs.get(0x33, 0) ^ r1) & ~0x0101 & 0xFFFF:
                regs[r0] = r1
            elif r0 in REFUSED:
                ret = -1
            else:
                regs[r0] = r1
        elif name == "delay_us":
            st["us"] += r0
        elif name == "rx_hz":
            ret = 440_850_000
        elif name == "ms":
            ret = st["us"] // 1000
        elif name == "scratch":
            ret = ram + 0x6000
            uc.mem_write(r0, spare.to_bytes(4, "little"))
        uc.reg_write(A.UC_ARM_REG_R0, ret & 0xFFFFFFFF)
    mu.hook_add(unicorn.UC_HOOK_CODE, on_stub, begin=stubs, end=stubs + 16 * len(NAMES))
    mu.reg_write(A.UC_ARM_REG_R0, api)
    mu.reg_write(A.UC_ARM_REG_SP, sp0)
    mu.reg_write(A.UC_ARM_REG_LR, stop | 1)
    mu.emu_start(base + entry | 1, stop, count=200_000_000)
    r = mu.reg_read(A.UC_ARM_REG_R0)
    return (r - (1 << 32) if r & 0x80000000 else r), st["said"], regs, st["writes"]


START = {0x34: 0x0000, 0x35: 0x0000, 0x33: 0xd300, 0x31: 0x0000, 0x04: 0x0000,
         0x67: (174 - 120) << 1}


def windows(said, tag):
    out = {}
    for ln in said:
        if ln.startswith(tag) and " e=" in ln:
            f = dict(kv.split("=") for kv in ln.split()[2:])
            out[ln.split(" 34=")[0]] = {k: int(v, 16 if k == "34" else 10) for k, v in f.items()}
    return out


@need_clang
def test_builds_fits_level_2():
    code, entry = k6mod.build(SRC)
    assert entry == 0 and k6mod.source_level(SRC) == 2
    assert 16 + len(code) + 8 <= 4096, len(code)


@need_clang
def test_finds_the_frame_clock_and_restores():
    ret, said, regs, writes = run(START)
    assert said[0].startswith("i2s start rssi=-120")
    assert said[1] == "i2s 31=0000 33=d300 34=0000 35=0000 04=0000"
    assert said[2] == "i2s 24=0000 25=0000 26=0000 27=0000 28=0000"
    assert said[3] == "pb moder=00000000 pupdr=00000000 idr=0012"
    assert "i2s 33 d300 -> d200" in said
    assert len([s for s in said if s.startswith("r")]) == 16
    sweep = windows(said, "sel")
    assert len(sweep) == 16
    assert sweep["sel f"]["e"] > 1000 and all(sweep[f"sel {s:x}"]["e"] == 0 for s in range(15))
    en = [ln for ln in said if ln.startswith("en 31 34=000d")]
    assert en, said
    w = {k: int(v, 16 if k == "34" else 10) for k, v in (kv.split("=") for kv in en[0].split()[2:])}
    assert w["34"] == 0x000d and abs(w["e"] - w["n"] // PERIOD) <= 1 and w["t"] >= 1000
    d = said[said.index(en[0]) + 1]
    assert d.startswith("d ") and set(d.split()[1:]) == {str(PERIOD)}
    assert ret == w["e"]
    assert said[-1] == f"i2s done 34=0000 31=0000 33=d300 best={ret}"
    assert regs[0x34] == 0 and regs[0x31] == 0 and regs[0x04] == 0 and regs[0x33] == 0xd300
    assert {r for r, _ in writes} <= {0x34, 0x31, 0x04, 0x33}
    assert {v for r, v in writes if r == 0x33} == {0xd200, 0xd300}
    # only GP0's nibble ever changes
    assert all(v & 0xFFF0 == 0x0000 for r, v in writes if r == 0x34)


@need_clang
def test_no_writes_unless_safe():
    ret, said, regs, writes = run(START, moder=0x4)          # PB1 an output
    assert ret == -4 and writes == [] and "not an input" in said[-1]
    ret, said, regs, writes = run(START, gp0_gate=False)    # 6473ca7: REG_33 refused
    assert ret == -5 and writes == [(0x33, 0xd200)] and "REG_33 bit 8" in said[-1]
    assert regs[0x33] == 0xd300
    base = [ln for ln in said if ln.startswith("base")][0]   # the pull-up: high, no edges
    assert " e=0 " in base and base.split(" n=")[1].split()[0] == base.split(" h=")[1].split()[0]


def cap_model(sel, reads, regs, us):
    """12: a clock, 3 samples a half period; 13: one high sample in 32 (a frame pulse); with
    REG_31 bit 0 a 64-sample frame; 14: 5 high, 7 low; 15: every sample; 9: every 20,000"""
    if sel == 12:
        return (reads // 3) & 1
    if sel == 13:
        return 1 if reads % (64 if regs.get(0x31, 0) & 1 else 32) == 0 else 0
    if sel == 14:
        return 1 if reads % 12 < 5 else 0
    if sel == 15:
        return reads & 1
    if sel == 9:
        return (reads // 20000) & 1
    return 0


@need_clang
def test_cap_builds_fits_level_2():
    code, entry = k6mod.build(CAP)
    assert entry == 0 and k6mod.source_level(CAP) == 2
    assert 16 + len(code) + 8 + 256 <= 4096, len(code)     # room for a 64-word capture at least


@need_clang
def test_cap_run_lengths():
    ret, said, regs, writes = run(START, src=CAP, model=cap_model, spare=1424)
    assert ret == 356, said
    assert said[0].startswith("cap start 33=d300 words=356 caps=")
    by = {}
    tag = None
    for ln in said[1:]:
        if ln.startswith("cap ") and " runs=" in ln:
            tag = ln.split()[1]
            by[tag] = [ln]
        elif tag:
            by[tag].append(ln)
    assert set(by) >= {"12", "13", "14", "15", "13+"}
    assert by["12"][1] == " hi 3:" + str(int(by["12"][1].split(":")[1])) and by["12"][2].startswith(" lo 3:")
    assert by["13"][1].startswith(" hi 1:") and by["13"][2].startswith(" lo 31:")
    assert by["13+"][2].startswith(" lo 32:")             # 63 low samples count as 32+
    assert by["14"][1].startswith(" hi 5:") and by["14"][2].startswith(" lo 7:")
    assert by["15"][1].startswith(" hi 1:") and by["15"][2].startswith(" lo 1:")
    assert " max=1/31" in by["13"][0]
    slow = [ln for ln in said if ln.startswith("slow 09")][0]
    assert " e=" in slow and int(slow.split(" e=")[1].split()[0]) > 2
    d = said[said.index(slow) + 1]
    assert set(d.split()[1:]) == {"20000"}
    assert said[-1] == "cap done 34=0000 31=0000 33=d300"
    assert regs[0x33] == 0xd300 and regs[0x34] == 0 and regs[0x31] == 0
    assert {r for r, _ in writes} <= {0x34, 0x31, 0x33}


@need_clang
def test_cap_refused_on_old_image():
    ret, said, regs, writes = run(START, src=CAP, model=cap_model, spare=1424, gp0_gate=False)
    assert ret == -5 and "PR #68" in said[-1] and regs[0x33] == 0xd300


def rssi_reg(dbm):
    return (dbm + 174) << 1


def tone_model(sel, reads, regs, us):
    """a carrier from 3 s to 8 s: code 11 then toggles every 3650 reads (the sign of a tone);
    off it, and code 9 always, every 40,000; code 13 is half ones"""
    if sel == 11 and 3_000_000 <= us < 8_000_000:
        return (reads // 3650) & 1
    if sel in (9, 11):
        return (reads // 40000) & 1
    if sel == 13:
        return reads & 1
    return 0


@need_clang
def test_tone_follows_the_carrier():
    regs = {**START, 0x67: lambda us: rssi_reg(-50 if 3_000_000 <= us < 8_000_000 else -105)}
    ret, said, regs, writes = run(regs, src=TONE, model=tone_model, spare=2128)
    assert said[0] == "tone start floor=-105 words=375"
    assert "tone waiting for a carrier" in said
    cyc = [ln for ln in said if ln.startswith("c ") and " 11 e=" in ln]
    on = [ln for ln in cyc if "rssi=-50" in ln]
    assert len(cyc) == ret and ret > 4 and len(on) == ret - 2, said
    g = [ln for ln in said if ln.startswith("c 3 11 g")][0]
    assert set(g.split()[4:]) == {"3650"}
    dens = [ln for ln in said if ln.startswith("c 0 13 d")]
    assert dens and set(" ".join(dens).replace("c 0 13 d", "").split()) == {"128"}
    assert said[-1] == f"tone done cycles={ret}"
    assert regs[0x33] == 0xd300 and regs[0x34] == 0
    assert {r for r, _ in writes} <= {0x34, 0x33}


FM = os.path.join(ROOT, "c", "k6", "mod", "gp0fm.c")


def fm_model():
    """code 13: a first-order sigma-delta of 0.5 + 0.35 sin(2 pi 17 kHz t) while the carrier is
    up (3 s on), 50% otherwise; t = PB1 reads / 24 MHz"""
    import math
    st = {"acc": 0.0}

    def model(sel, reads, regs, us):
        if sel != 13:
            return 0
        d = 0.5
        if us >= 3_000_000:
            d += 0.35 * math.sin(2 * math.pi * 17000 * reads / 24e6)
        st["acc"] += d
        if st["acc"] >= 1:
            st["acc"] -= 1
            return 1
        return 0
    return model


@need_clang
def test_fm_records_the_if():
    regs = {**START, 0x67: lambda us: rssi_reg(-21 if us >= 3_000_000 else -103)}
    ret, said, regs, writes = run(regs, src=FM, model=fm_model(), spare=256)
    assert ret == 256 and said[0].startswith("fm start floor=-103 bytes=256 cal caps=10 t=")
    assert said[1] == "fm waiting for a carrier"
    for k in range(2):
        rows = [ln for ln in said if ln.startswith(f"x {k} ")]
        assert len(rows) == 11 and all(len(r) <= 60 for r in rows)
        data = bytes.fromhex("".join(r.split()[3] for r in rows))
        assert len(data) == 256 and max(data) <= 128
        x = [v - 64 for v in data]
        zc = sum(1 for a, b in zip(x, x[1:]) if (a < 0) != (b < 0))
        # 256 outputs of 256 reads at 24 MHz = 2.73 ms of 17 kHz: about 93 crossings
        assert 80 <= zc <= 105, zc
    assert said[-1] == "fm done"
    assert regs[0x33] == 0xd300 and regs[0x34] == 0


@need_clang
def test_raw_records_the_bitstream():
    """gp0raw: the same run, but each capture holds the raw samples, first in bit 31 of each
    little-endian word"""
    raw = os.path.join(ROOT, "c", "k6", "mod", "gp0raw.c")
    regs = {**START, 0x67: lambda us: rssi_reg(-21 if us >= 3_000_000 else -103)}
    ret, said, regs, writes = run(regs, src=raw, model=fm_model(), spare=1600)
    assert ret == 1600 and said[0].startswith("fm start floor=-103 bytes=1600")
    rows = [ln for ln in said if ln.startswith("x 1 ")]
    data = bytes.fromhex("".join(r.split()[3] for r in rows))
    words = [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]
    bits = [(w >> (31 - b)) & 1 for w in words for b in range(32)]
    assert len(bits) == 12800 and 0.4 < sum(bits) / len(bits) < 0.6
    dens = [sum(bits[i:i + 256]) - 128 for i in range(0, len(bits), 256)]
    zc = sum(1 for a, b in zip(dens, dens[1:]) if (a < 0) != (b < 0))
    # 12800 reads at 24 MHz = 0.53 ms of 17 kHz: about 18 crossings
    assert 14 <= zc <= 22, (zc, dens)
    assert said[-1] == "fm done" and regs[0x33] == 0xd300 and regs[0x34] == 0
    assert {r for r, _ in writes} <= {0x34, 0x33}


DMA_SRC = os.path.join(ROOT, "c", "k6", "mod", "gp0dma.c")


def dma_bench(f_if=17000.0, dev=4000.0, f_tone=1000.0, rcc_apb2=0, ccr1=0):
    """RCC, TIM17 and DMA channel 1 as the module uses them: each read of CNDTR1 while the timer
    and channel run moves the DMA on by half an output's worth of transfers, writing GPIOB IDR
    bytes (PB4 high, PB1 a first-order sigma-delta of an FM IF) into the ring"""
    import numpy as np
    rcc = {0x14: 0x14, 0x18: rcc_apb2}
    tim, dma = {}, {0x08: ccr1}
    sim = {"t": 0.0, "acc": 0.0, "phase": 0.0, "written": 0, "regs_seen": set()}

    def rcc_r(uc, off, size, st):
        return rcc.get(off, 0)

    def rcc_w(uc, off, size, value, st):
        rcc[off] = value

    def tim_r(uc, off, size, st):
        return tim.get(off, 0)

    def tim_w(uc, off, size, value, st):
        assert rcc[0x18] & (1 << 18), "TIM17 written with its clock off"
        tim[off] = value

    def produce(uc, n):
        arr = tim.get(0x2C, 0xFFFF)
        fs = 96e6 / (arr + 1)
        t = sim["t"] + np.arange(n) / fs
        ph = sim["phase"] + 2 * np.pi * np.cumsum(f_if + dev * np.sin(2 * np.pi * f_tone * t)) / fs
        d = 0.5 + 0.3 * np.sin(ph)
        acc = sim["acc"] + np.cumsum(d)
        bits = np.diff(np.floor(np.concatenate(([sim["acc"]], acc)))).astype(np.uint8)
        sim["t"] += n / fs
        sim["phase"] = ph[-1]
        sim["acc"] = acc[-1] - np.floor(acc[-1])
        return bytes((0x10 | (bits << 1)).tolist())

    def dma_r(uc, off, size, st):
        if off == 0x0C and dma.get(0x08, 0) & 1 and tim.get(0x00, 0) & 1 and \
                tim.get(0x0C, 0) & 0x100:
            ring, n = dma[0x14], dma[0x0C]
            L = 1024 // (tim[0x2C] + 1)
            step = max(L // 2, 4)
            data = produce(uc, step)
            pos = 1024 - n
            first = min(step, 1024 - pos)
            uc.mem_write(ring + pos, data[:first])
            if first < step:
                uc.mem_write(ring, data[first:])
            n -= step
            while n <= 0:
                n += 1024
            dma[0x0C] = n
            sim["written"] += step
        return dma.get(off, 0)

    def dma_w(uc, off, size, value, st):
        assert rcc[0x14] & 1, "DMA written with its clock off"
        if off == 0x04:
            return
        dma[off] = value
        if off == 0x08 and value & 1:
            assert dma[0x10] == 0x48000410 and dma[0x0C] == 1024
            assert value & 0xFFE == (3 << 12 | 1 << 7 | 1 << 5) & 0xFFE, hex(value)
    mmio = [(0x40021000, 0x400, rcc_r, rcc_w), (0x40014800, 0x400, tim_r, tim_w),
            (0x40020000, 0x400, dma_r, dma_w)]
    return mmio, rcc, tim, dma, sim


def dma_build_small(tmp_path):
    w = tmp_path / "gp0dma_small.c"
    w.write_text(f'#define BLK 940u\n#define NBLK 3\n#define CAL_SPINS 500u\n#include "{DMA_SRC}"\n')
    return str(w)


@need_clang
def test_dma_demodulates_on_the_mcu(tmp_path):
    mmio, rcc, tim, dma, sim = dma_bench()
    regs = {**START, 0x67: lambda us: rssi_reg(-21 if us >= 3_000_000 else -103)}
    ret, said, regs, writes = run(regs, src=dma_build_small(tmp_path), spare=1100, mmio=mmio)
    assert ret == 5, said
    assert said[0].startswith("dma start floor=-103 ring=1024 s0=")
    assert "dma waiting for a carrier" in said and said[-1] == "dma done"
    runs = [ln for ln in said if ln.startswith("r ")]
    assert [ln.split()[1] for ln in runs] == ["24000", "12000", "6000", "3000", "1500"] * 2
    for rate in ("24000", "12000", "6000", "3000"):
        bl = [dict(kv.split("=") for kv in ln.split()[3:]) for ln in said
              if ln.startswith(f"b {rate} ")][1:]          # the carrier pass: 3 blocks
        assert len(bl) == 3, said
        for b in bl:
            assert b["lost"] == "0"
            assert 16500 <= int(b["if"]) <= 17500, (rate, b)
            assert 3000 <= int(b["dev"]) <= 5000, (rate, b)
        for b in bl[1:]:
            assert 850 <= int(b["tone"]) <= 1150, (rate, b)
    assert all(len(ln) <= 60 for ln in said)
    # everything put back: clocks, timer, channel, chip registers
    assert rcc[0x18] == 0 and rcc[0x14] == 0x14
    assert tim[0x00] == 0 and tim[0x0C] == 0 and dma[0x08] == 0
    assert regs[0x33] == 0xd300 and regs[0x34] == 0
    assert {r for r, _ in writes} <= {0x34, 0x33}


@need_clang
def test_dma_leaves_a_busy_timer_alone(tmp_path):
    for apb2, ccr1 in ((1 << 18, 0), (0, 1)):
        mmio, rcc, tim, dma, sim = dma_bench(rcc_apb2=apb2, ccr1=ccr1)
        ret, said, regs, writes = run(START, src=dma_build_small(tmp_path), spare=1100, mmio=mmio)
        assert ret == -7 and "busy" in said[-1] and not writes and not tim
        assert rcc[0x18] == apb2 and dma[0x08] == ccr1
