"""The K6 FT8 receiver (docs/k6-ft8-wspr.md): the fixed-point core (c/k6/ft8/ft8k.c) on the host
against the Python reference encoder (sim/k6digi/ft8ref.py), the K6 GP0 path in simulation, the
host decode tool (tools/k6_ft8.py), and the ft8wf module on an emulated Cortex-M0 fed a modelled
DMA ring."""
import base64
import os
import shutil
import subprocess
import sys

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sim.k6digi import ft8ref, ft8_wav, gp0  # noqa: E402

MOD = os.path.join(ROOT, "c", "k6", "mod")
need_cc = pytest.mark.skipif(not shutil.which("cc"), reason="needs a C compiler")
need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")
FS = 12000


def audio(texts, f0=1512.0, snr=None, seed=1, start=0.7):
    """one transmission every 15 s, power 1 while on; noise for snr dB in 2500 Hz"""
    rng = np.random.default_rng(seed)
    x = np.zeros(int((15 * len(texts) + 0.5) * FS))
    for i, t in enumerate(texts):
        _, tones = ft8ref.encode(t)
        ph, env = ft8ref.gfsk_phase(tones, FS, f0)
        s = int((15 * i + start) * FS)
        x[s:s + len(ph)] += np.sqrt(2) * np.cos(ph + rng.uniform(0, 6.3)) * env
    if snr is not None:
        x += rng.normal(0, np.sqrt(FS / 2 / 2500 / 10 ** (snr / 10)), len(x))
    return x


def test_reference_encoder():
    b, tones = ft8ref.encode("CQ AG7EW CN87")
    assert ft8ref.unpack_text(b) == "CQ AG7EW CN87"
    assert list(tones[:7]) == ft8ref.COSTAS == list(tones[36:43]) == list(tones[72:])
    assert len(tones) == 79 and tones.max() <= 7
    for t in ("K1ABC W1AW R-07", "W1AW AG7EW RR73", "VE9QRP KA7ABC -12", "AG7EW W7AAA 73"):
        assert ft8ref.unpack_text(ft8ref.encode(t)[0]) == t


@need_cc
def test_decodes_messages_in_order(tmp_path):
    texts = ["CQ AG7EW CN87", "K1ABC W1AW R-07", "W1AW AG7EW RR73", "VE9QRP KA7ABC -12"]
    res = ft8_wav.run_iq(ft8_wav.to_iq(audio(texts, snr=-10), FS, 1520.0), 32,
                         ["wfout=%s" % (tmp_path / "w.bin")])
    assert [d["text"] for d in res] == texts
    for i, d in enumerate(res):
        assert abs(d["row"] * 0.08 - (15 * i + 0.7)) < 0.1, d      # symbol 0's time
        assert abs(1520.0 + (d["bin"] - 16) * 6.25 - 1512.0) <= 6.25, d  # tone 0, to a bin
    # the same rows as ft8wf sends them decode the same through the wf path
    out = subprocess.run([ft8_wav.host(), "wf", str(tmp_path / "w.bin"), "32"], check=True,
                         capture_output=True, text=True).stdout
    assert [ln.split(None, 5)[5] for ln in out.splitlines()] == texts


@need_cc
def test_noise_alone_decodes_nothing():
    rng = np.random.default_rng(7)
    x = rng.normal(0, 1, 60 * FS)
    assert ft8_wav.run_iq(ft8_wav.to_iq(x, FS, 1520.0), 32) == []


@need_cc
@pytest.mark.parametrize("inv", [0, 1])
def test_k6_if_path(inv):
    """USB audio -> the K6's IF at -12 dB with the noise-filling gain -> 1-bit sigma-delta ->
    counts -> the receiver mixing at the IF +- 1500 Hz"""
    x = audio(["CQ AG7EW CN87"], snr=None)
    sig = gp0.ssb(x, FS, 48000)
    if inv:
        sig = np.conj(sig)
    z = gp0.k6_if(sig, 48000, -12, floor=0.15, seed=3)
    c = gp0.counts(z, 48000, seed=3)
    fmix = gp0.FIF - 1520 if inv else gp0.FIF + 1520
    res = gp0.run_counts(c, 32, fmix, ["1"] if inv else ["0"])
    assert [d["text"] for d in res] == ["CQ AG7EW CN87"]


def ft8wf_log(rows, nb=32, drop=()):
    rb = nb // 2
    out = ["K6 MOD say ft8wf start nb=%d f=1520 inv=0" % nb,
           "K6 MOD say ft8wf gain rms=21.4 r2=27 ks=5490"]
    for i in range(0, len(rows) // rb - 1, 2):
        if i not in drop:
            out.append("K6 MOD say w %d %s" % (i, base64.b64encode(rows[i * rb:(i + 2) * rb]).decode()))
    out.append("K6 MOD say ft8wf done rows=%d lost=0" % (len(rows) // rb))
    return "\n".join(out) + "\n"


@need_cc
def test_tool_decodes_a_log_with_lost_lines(tmp_path):
    ft8_wav.run_iq(ft8_wav.to_iq(audio(["CQ AG7EW CN87"], snr=-12), FS, 1520.0), 32,
                   ["wfout=%s" % (tmp_path / "w.bin")])
    log = tmp_path / "k6.log"
    log.write_text(ft8wf_log(open(tmp_path / "w.bin", "rb").read(), drop=(40, 90)))
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_ft8
    start, rows, lost = k6_ft8.parse(str(log))
    assert lost == 4 and start["nb"] == "32" and start["r2"] == "27"
    res = k6_ft8.decode(start, rows)
    assert [d["text"] for d in res] == ["CQ AG7EW CN87"]
    assert abs(res[0]["f"] - 1512.0) <= 6.25 and abs(res[0]["t"] - 0.7) < 0.1


# --- the modules on an emulated M0 ----------------------------------------------------------

NAMES = ["say", "ticks", "fd_read", "fd_write", "delay_us", "tune", "rx_hz", "ms", "key_down",
         "scratch"]


def k6_bench(sig, regs, fdma=5.71e6, step=128):
    """RCC, TIM14, TIM17, DMA channel 1 and GPIOB as gp0rx.h uses them. The channel runs flat
    out at fdma: each read of CNDTR1 moves it on by `step` transfers of GPIOB IDR bytes (PB4
    high, PB1 a first-order sigma-delta of sig(t, regs) in 0..1) into the ring, and the clock
    moves with it: TIM17 at 96 MHz, TIM14 in 100 us ticks (time stands still while the CPU
    works, so no counts are lost here)"""
    rcc = {0x14: 0x14, 0x18: 0}
    tim, dma = {}, {}
    sim = {"t": 0.0, "acc": 0.0, "n": 0}

    def rcc_r(uc, off, size, st):
        return rcc.get(off, 0)

    def rcc_w(uc, off, size, value, st):
        rcc[off] = value

    def tim_r(uc, off, size, st):
        return int(sim["t"] * 96e6) & 0xFFFF if off == 0x24 else tim.get(off, 0)

    def tim_w(uc, off, size, value, st):
        assert rcc[0x18] & (1 << 18), "TIM17 written with its clock off"
        tim[off] = value

    def t14_r(uc, off, size, st):
        assert off == 0x24, hex(off)
        return int(sim["t"] * 1e4) & 0xFFFF

    def t14_w(uc, off, size, value, st):
        raise AssertionError("module wrote TIM14")

    def dma_r(uc, off, size, st):
        if not rcc[0x14] & 1:
            return 0x37
        if off == 0x0C and dma.get(0x08, 0) & 1 and tim.get(0x00, 0) & 1:
            t = sim["t"] + np.arange(step) / fdma
            d = np.clip(sig(t, st["regs"]), 0, 1)
            acc = sim["acc"] + np.cumsum(d)
            bits = np.diff(np.floor(np.concatenate(([sim["acc"]], acc)))).astype(np.uint8)
            sim["acc"] = acc[-1] - np.floor(acc[-1])
            sim["t"] += step / fdma
            sim["n"] += step
            data = bytes((0x10 | (bits << 1)).tolist())
            ring, n, rsz = dma[0x14], dma[0x0C], sim["rsz"]
            pos = rsz - n
            first = min(step, rsz - pos)
            uc.mem_write(ring + pos, data[:first])
            if first < step:
                uc.mem_write(ring, data[first:])
            n -= step
            while n <= 0:
                n += rsz
            dma[0x0C] = n
        return dma.get(off, 0)

    def dma_w(uc, off, size, value, st):
        assert rcc[0x14] & 1, "DMA written with its clock off"
        if off == 0x04:
            return
        dma[off] = value
        if off == 0x08 and value & 1:
            assert dma[0x10] == 0x48000410 and dma[0x0C] in (512, 1024)
            sim["rsz"] = dma[0x0C]
            assert value & 0xFFE == (3 << 12 | 1 << 7 | 1 << 5) & 0xFFE, hex(value)

    def gpio_r(uc, off, size, st):
        assert off == 0x00, "read GPIOB+%#x" % off
        return 0

    def gpio_w(uc, off, size, value, st):
        raise AssertionError("module wrote GPIOB")
    mmio = [(0x40021000, rcc_r, rcc_w), (0x40014800, tim_r, tim_w), (0x40020000, dma_r, dma_w),
            (0x40002000, t14_r, t14_w), (0x48000400, gpio_r, gpio_w)]
    return mmio, rcc, tim, dma, sim


def emulate(src, regs, sig):
    """run module src on an emulated M0. Returns (ret, said, regs, writes, stack bytes, bench)"""
    unicorn = pytest.importorskip("unicorn")
    from unicorn import arm_const as A
    sys.path.insert(0, ROOT)
    from modem import k6mod
    mmio, rcc, tim, dma, sim = k6_bench(sig, regs)
    st = {"us": 0, "said": [], "writes": [], "regs": dict(regs)}
    regs = st["regs"]
    mu = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB | unicorn.UC_MODE_MCLASS)
    mu.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M0)
    ram = 0x20000000
    mu.mem_map(ram, 1 << 20)
    base, api, stubs, stop, sp0 = ram + 0x1000, ram + 0x8000, ram + 0x9000, ram + 0x9F00, ram + 0x7F00
    code, entry = k6mod.build(src)
    assert len(code) + 24 <= 4096, len(code)
    mu.mem_write(base, code)
    mu.mem_write(ram + 0x3000, b"\xa5" * (sp0 - ram - 0x3000))
    for i in range(len(NAMES)):
        mu.mem_write(stubs + 16 * i, b"\x70\x47")
    mu.mem_write(stop, b"\x00\xbf" * 8)
    mu.mem_write(api, (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
                 + b"".join((stubs + 16 * i | 1).to_bytes(4, "little") for i in range(len(NAMES))))
    for b, r, w in mmio:
        mu.mmio_map(b, 0x400, r, st, w, st)

    def on_stub(uc, addr, size, _):
        name = NAMES[(addr - stubs) // 16]
        r0, r1 = uc.reg_read(A.UC_ARM_REG_R0), uc.reg_read(A.UC_ARM_REG_R1)
        ret = 0
        if name == "say":
            st["said"].append(bytes(uc.mem_read(r0, 64)).split(b"\0")[0].decode())
            assert len(st["said"][-1]) <= 60, st["said"][-1]
        elif name == "fd_read":
            ret = regs.get(r0, 0)
        elif name == "fd_write":
            st["writes"].append((r0, r1))
            if r0 == 0x33 and not (regs.get(0x33, 0) ^ r1) & ~0x0101 & 0xFFFF:
                regs[r0] = r1
            elif r0 in (0x30, 0x33, 0x36, 0x37):
                ret = -1
            else:
                regs[r0] = r1
        elif name == "delay_us":
            st["us"] += r0
        elif name == "ms":
            ret = st["us"] // 1000
        elif name == "rx_hz":
            ret = 440_850_000
        elif name == "scratch":
            ret = ram + 0x6000
            uc.mem_write(r0, (400).to_bytes(4, "little"))
        uc.reg_write(A.UC_ARM_REG_R0, ret & 0xFFFFFFFF)
    mu.hook_add(unicorn.UC_HOOK_CODE, on_stub, begin=stubs, end=stubs + 16 * len(NAMES))
    mu.reg_write(A.UC_ARM_REG_R0, api)
    mu.reg_write(A.UC_ARM_REG_SP, sp0)
    mu.reg_write(A.UC_ARM_REG_LR, stop | 1)
    mu.emu_start(base + entry | 1, stop, count=2_000_000_000)
    r = mu.reg_read(A.UC_ARM_REG_R0)
    mem = bytes(mu.mem_read(ram + 0x3000, sp0 - ram - 0x3000))
    used = len(mem) - next(i for i, v in enumerate(mem) if v != 0xa5)
    return ((r - (1 << 32) if r & 0x80000000 else r), st["said"], regs, st["writes"], used,
            (rcc, tim, dma, sim))


START = {0x34: 0x0000, 0x33: 0xd300, 0x73: 0x4691, 0x7E: 0x302E, 0x13: 0x03DF,
         0x67: (174 - 120) << 1}
TONE = gp0.FIF + 1520 + 25.0                    # 4 bins above the window's centre: bin 20


@need_clang
def test_ft8rx_builds():
    """the whole receiver on the K6: position-independent and linked, but over today's 4 KB
    store (docs/k6-ft8-wspr.md: it waits for a bigger module arena)"""
    from modem import k6mod
    code, entry = k6mod.build(os.path.join(MOD, "ft8rx.c"))
    assert 6000 < len(code) < 8192, len(code)


def ft8wf_short(tmp_path, seconds=3):
    w = tmp_path / "ft8wf_short.c"
    w.write_text('#define K6_MODULE_OPT z\n#define FT8_SECONDS %d\n#define FT8_AUDIO 1520\n'
                 '#include "%s"\n'
                 % (seconds, os.path.join(MOD, "ft8wf.c")))
    return str(w)


def rows_of(said, nb=32):
    rows = []
    for ln in said:
        if ln.startswith("w "):
            b = base64.b64decode(ln.split()[2])
            assert len(b) == nb
            for r in (b[:nb // 2], b[nb // 2:]):
                rows.append([v for x in r for v in (x & 15, x >> 4)])
    return np.array(rows)


@need_clang
def test_ft8wf_streams_a_tone(tmp_path):
    """a steady tone 25 Hz above the window's centre, loud enough that the gain stays: rows of
    32 bins with the tone at bin 20 and the floor elsewhere; everything put back"""
    pytest.importorskip("unicorn")

    def sig(t, regs):
        return 0.5 + 0.3 * np.cos(2 * np.pi * TONE * t)
    ret, said, regs, writes, stack, (rcc, tim, dma, sim) = emulate(ft8wf_short(tmp_path), START, sig)
    assert said[0] == "ft8wf start nb=32 f=1520 inv=0", said
    assert said[1].startswith("ft8wf gain rms=") and " g=" not in said[1], said
    f = dict(kv.split("=") for kv in said[1].split()[2:])
    assert f["r2"] in ("27", "28") and 5600 <= int(f["ks"]) <= 5800, f
    seq = [int(ln.split()[1]) for ln in said if ln.startswith("w ")]
    assert seq == list(range(0, 2 * len(seq), 2)) and len(seq) >= 10, said
    assert said[-1] == "ft8wf done rows=%d lost=0" % (2 * len(seq)) and ret == 2 * len(seq)
    w = rows_of(said)[4:]                           # after the CICs and the DFT fill
    assert (w.argmax(axis=1) == 20).all(), w
    assert (w[:, 20] >= 12).all() and np.median(np.delete(w, [19, 20, 21], axis=1)) <= 5, w
    assert stack < 2600, stack
    print("ft8wf stack", stack)
    # put back: clocks, timer, channel, chip registers; only the documented ones written
    assert rcc[0x18] == 0 and rcc[0x14] == 0x14 and tim[0x00] == 0 and dma[0x08] == 0
    assert all(regs[r] == v for r, v in START.items()), regs
    assert {r for r, _ in writes} <= {0x33, 0x34, 0x73, 0x7E, 0x13}
    assert (0x34, 13) in writes and (0x73, 0x4691 | 0x10) in writes


@need_clang
def test_ft8wf_fills_the_one_bit_path(tmp_path):
    """receiver noise too quiet for the 1-bit path at the pager's AGC: the module fixes the AGC
    and takes the first REG_13 gain whose noise is under 14 counts rms (the modelled chip's
    noise grows with the gain), then puts the AGC back at the end"""
    pytest.importorskip("unicorn")
    rng = np.random.default_rng(5)
    # the noise as a tone well off the window (an rms the 1-bit path keeps), louder with gain
    amp = {0x03FF: 0.25, 0x03BE: 0.12, 0x037E: 0.08, 0x033E: 0.05}

    def sig(t, regs):
        a = amp.get(regs[0x13], 0.02) if regs[0x7E] & 0x8000 else 0.02
        return 0.5 + a * np.cos(2 * np.pi * (gp0.FIF + 6000) * t) + rng.normal(0, 0.01, len(t))
    ret, said, regs, writes, stack, _ = emulate(ft8wf_short(tmp_path, 1), START, sig)
    assert " g=03be rms=" in said[1], said[1]
    rms = [float(kv.split("=")[1]) for kv in said[1].split() if kv.startswith("rms=")]
    assert rms[0] < 8.0 and rms[1] <= 14.0, said[1]
    assert (0x7E, (0x302E & 0x0FFF) | 0x8000 | 3 << 12) in writes and (0x13, 0x03FF) in writes
    assert regs[0x7E] == 0x302E and regs[0x13] == 0x03DF
    assert said[-1].startswith("ft8wf done") and ret > 0


@need_clang
def test_ft8wf_whole_chain_decodes(tmp_path):
    """an FT8 transmission at the K6's IF (USB, tone 0 at 1512 Hz) through the 1-bit path, the
    emulated module's DMA ring, mixer, CICs and DFT, its `w` lines, and the host decoder"""
    pytest.importorskip("unicorn")
    _, tones = ft8ref.encode("CQ AG7EW CN87")
    fa = 12000
    ph, env = ft8ref.gfsk_phase(tones, fa, 1512.0)
    tt = np.arange(len(ph)) / fa
    rng = np.random.default_rng(9)
    t0 = 1.0                                     # after the module's ~0.4 s set-up

    def sig(t, regs):
        u = t - t0
        on = (u >= 0) & (u < tt[-1])
        p = np.interp(u, tt, ph) + 2 * np.pi * gp0.FIF * t
        return 0.5 + 0.15 * on * np.interp(u, tt, env) * np.cos(p) + rng.normal(0, 0.05, len(t))
    ret, said, regs, writes, stack, _ = emulate(ft8wf_short(tmp_path, 15), START, sig)
    log = tmp_path / "k6.log"
    log.write_text("\n".join(said) + "\n")
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_ft8
    start, rows, lost = k6_ft8.parse(str(log))
    assert lost == 0
    res = k6_ft8.decode(start, rows)
    assert [d["text"] for d in res] == ["CQ AG7EW CN87"], said[:3]
    assert abs(res[0]["f"] - 1512.0) <= 6.25, res
