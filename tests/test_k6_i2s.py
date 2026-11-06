"""The I2S probe module (c/k6/mod/i2sprobe.c) on an emulated Cortex-M0, against a modelled
FD6818 whose GP0 drives the MCU's PB1."""
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "c", "k6", "mod", "i2sprobe.c")
sys.path.insert(0, ROOT)
from modem import k6mod  # noqa: E402

need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")
NAMES = ["say", "ticks", "fd_read", "fd_write", "delay_us", "tune", "rx_hz", "ms", "key_down",
         "scratch"]
REFUSED = (0x30, 0x33, 0x36, 0x37)
PERIOD = 400            # samples between modelled DFS edges


def run(regs, moder=0, i2s_en=(0x31, 1)):
    """regs: the chip's registers at the start. GP0 follows REG_34[3:0]: 13 (DFS) toggles every
    PERIOD reads of PB1 once i2s_en's bit is set, 15 (XTAL/2) on every read, others hold 0.
    Returns (ret, said lines, registers at the end, writes)."""
    unicorn = pytest.importorskip("unicorn")
    from unicorn import arm_const as A
    st = {"us": 0, "said": [], "writes": [], "reads": 0, "pin": 0}
    regs = dict(regs)
    mu = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB | unicorn.UC_MODE_MCLASS)
    mu.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M0)
    ram = 0x20000000
    mu.mem_map(ram, 1 << 20)
    base, api, stubs, stop, sp0 = ram + 0x1000, ram + 0x8000, ram + 0x9000, ram + 0x9F00, ram + 0x7F00
    code, entry = k6mod.build(SRC)
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
            if sel == 15:
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
        elif name == "fd_write":
            st["writes"].append((r0, r1))
            if r0 in REFUSED:
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
            uc.mem_write(r0, (1024).to_bytes(4, "little"))
        uc.reg_write(A.UC_ARM_REG_R0, ret & 0xFFFFFFFF)
    mu.hook_add(unicorn.UC_HOOK_CODE, on_stub, begin=stubs, end=stubs + 16 * len(NAMES))
    mu.reg_write(A.UC_ARM_REG_R0, api)
    mu.reg_write(A.UC_ARM_REG_SP, sp0)
    mu.reg_write(A.UC_ARM_REG_LR, stop | 1)
    mu.emu_start(base + entry | 1, stop, count=200_000_000)
    r = mu.reg_read(A.UC_ARM_REG_R0)
    return (r - (1 << 32) if r & 0x80000000 else r), st["said"], regs, st["writes"]


START = {0x34: 0x0005, 0x35: 0x3200, 0x33: 0x0000, 0x31: 0x0000, 0x04: 0x0000,
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
    assert said[1] == "i2s 31=0000 33=0000 34=0005 35=3200 04=0000"
    assert said[2] == "i2s 24=0000 25=0000 26=0000 27=0000 28=0000"
    assert said[3] == "pb moder=00000000 pupdr=00000000 idr=0010"
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
    assert said[-1] == f"i2s done 34=0005 31=0000 04=0000 best dfs e={ret}"
    assert regs[0x34] == 0x0005 and regs[0x31] == 0 and regs[0x04] == 0
    assert {r for r, _ in writes} <= {0x34, 0x31, 0x04}
    # only GP0's nibble ever changes
    assert all(v & 0xFFF0 == 0x0000 for r, v in writes if r == 0x34)


@need_clang
def test_no_writes_unless_safe():
    ret, said, regs, writes = run(START, moder=0x4)          # PB1 an output
    assert ret == -4 and writes == [] and "not an input" in said[-1]
    ret, said, regs, writes = run({**START, 0x33: 0x0100})   # GP0 output disabled
    assert ret == -5 and writes == [] and "REG_33 bit 8" in said[-1]
