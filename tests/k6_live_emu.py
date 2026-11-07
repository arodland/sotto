"""The iflive module (c/k6/mod/iflive.c) on an emulated Cortex-M0 (unicorn) against a modelled
GP0 stream: the sender's live audio through the study's model (sim/k6if: the IC-7100, FM, the
FD6818's filter and AGC, the code 13 sigma-delta at the DMA's rate), as the bytes DMA channel 1
would write into the module's ring, paced by the emulated CPU's time. tests/test_k6_ifrx.py
uses it; run on its own for the full 10-step run:

  python3 tests/k6_live_emu.py [4fsk|ffsk] [--cn0 46] [--off -164] [--steps 10] [--cpi 1.5]
                               [--fdma 5.875e6] [--stall 50,400]
  python3 tests/k6_live_emu.py --module c/k6/mod/gp0dma.c      (any GP0 module on the same stream)
  python3 tests/k6_live_emu.py --module c/k6/mod/iflive.c --profile   (and where its time goes)

Time: the module runs in chunks of CHUNK instructions, each CPI cycles of the 96 MHz clock (the
module runs from RAM, no wait states; the M0 takes 1 to 3 cycles an instruction: 1.5 is a
middling mix of loads, multiplies and branches); the API's calls cost what the radio's do
(a line 5 ms, an FD6818 read 20 us, a key check 60 us: ft8wf's overruns on the K6 matched only
at that). The DMA and TIM17 read the clock at each register read.

Calibration: gp0dma (c/k6/mod/gp0dma.c), measured on Jeff's K6 at 72-73% idle with L = 64
(docs/k6-fd6818-audio.md, run 8), runs here at CPI 1.5 at 72% (L = 32: 40% here, 56% on the
K6; L = 128: 87%, 93%): `python3 tests/k6_live_emu.py --module c/k6/mod/gp0dma.c`. So 1.5 is
right for counting and loop code, and if anything pessimistic.
SPDX-License-Identifier: MIT"""
import argparse
import os
import shutil
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from modem import k6mod  # noqa: E402

LIVE = os.path.join(ROOT, "c", "k6", "mod", "iflive.c")
LIVE_FFSK = os.path.join(ROOT, "c", "k6", "mod", "iflive_ffsk.c")
FCPU = 96e6
CHUNK = 256


def build(mode, defs=(), src=None):
    """the module's code and entry, with #defines (name, value) ahead of its source (src: a
    variant of the mode's module, e.g. c/k6/mod/iflive_tsweep.c)"""
    src = os.path.abspath(src) if src else LIVE if mode == "4fsk" else LIVE_FFSK
    if not defs:
        return k6mod.build(src)
    with tempfile.TemporaryDirectory() as d:
        w = os.path.join(d, "w.c")
        with open(w, "w") as f:
            f.write("#define K6_MODULE_LEVEL 2\n#define K6_MODULE_OPT z\n")
            f.writelines(f"#define {k} {v}\n" for k, v in defs)
            f.write(f'#include "{src}"\n')
        return k6mod.build(w)


def stream(mode, cn0, f_off=0.0, steps=10, seg_s=6.0, lead_s=1.5, seed=1, pre_s=0.25, fdma=None,
           fine=False):
    """(bytes the DMA reads from GPIOB's IDR at its rate, bit 1 the code 13 output; the
    sample at which the carrier comes up). pre_s of nothing, then carrier and the live audio.
    fine: the sigma-delta's every bit at its own clock instead (FCLK), for a DMA whose reads
    fall where the emulated bus puts them (Emu's wobble); the rate returned is still fdma"""
    import k6_ifcap as ic
    from sim.k6if import mcu, study as st
    m = next(mm for n, mm in ic.PASSES.values() if n == mode)
    a = ic.live_audio(m, steps, seg_s, lead_s)
    n0 = int(0.2 * st.FSA)                      # key_and_play: carrier 0.2 s before the audio
    s = st.fm(st.ic7100(np.concatenate([np.zeros(n0), a])), f_off)
    fdma = fdma or mcu.FDMA                     # Jeff's K6's rate under gp0dma
    fs = st.fe.FCLK if fine else fdma
    bits = st.fe.receive(s, st.FZ, cn0, seed=seed, L=0, fdma=fs, **({"jit": 0.0} if fine else {}))
    lead = int(pre_s * fs)
    rng = np.random.default_rng(seed + 1)
    noise = rng.integers(0, 2, lead)            # no carrier: the squelched IF's idle toggling
    if fine:
        return np.concatenate([noise, bits]).astype(np.uint8) * 2, int(pre_s * fdma), fdma
    return np.concatenate([noise, bits]).astype(np.uint8) * 2, lead, fdma


class Emu:
    def __init__(self, code, entry, ib, on_at, fdma, cpi=1.5, max_s=None, key_us=60, stall=None,
                 wobble=None):
        import unicorn
        from unicorn import arm_const as A
        self.U, self.A = unicorn, A
        self.ib, self.on_at, self.fdma, self.cpi = ib, on_at, fdma, cpi
        self.key_us, self.stall, self.keys = key_us, stall, 0
        # wobble w: ib holds the sigma-delta's bits at FCLK, and the DMA runs at fdma while the
        # CPU computes and fdma * (1 + w) while it polls the ring (a chunk with 3 or more reads
        # of CNDTR): the K6's channel 1 runs at what the bus leaves it
        self.wob, self.fhi = wobble, 6.5e6
        self.max_cyc = (max_s or (len(ib) / (self.fhi if wobble is not None else fdma) + 1.0)) * FCPU
        self.cyc = 0.0
        self.spos, self.nread, self.bp = 0.0, 0, [(0.0, 0.0)]
        self.said, self.writes, self.reg = [], {}, {}
        self.dma_on, self.dma_t0, self.dma_s0, self.made = False, 0.0, 0, 0
        mu = self.mu = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB | unicorn.UC_MODE_MCLASS)
        mu.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M0)
        ram = self.ram = 0x20000000
        mu.mem_map(ram, 1 << 20)
        self.base, self.api, self.stubs = ram + 0x1000, ram + 0x8000, ram + 0x9000
        self.stop, self.sp0, self.stack_lo = ram + 0x9F00, ram + 0x7F00, ram + 0x7F00 - 3504
        mu.mem_write(self.base, code)
        mu.mem_write(self.stack_lo - 512, b"\xa5" * (3504 + 512))
        self.names = ["say", "ticks", "fd_read", "fd_write", "delay_us", "tune", "rx_hz", "ms",
                      "key_down", "scratch", "tx_on", "tx_off", "tx_pa", "key_poll", "cal_read",
                      "cal_write"]
        for i in range(len(self.names)):
            mu.mem_write(self.stubs + 16 * i, b"\x70\x47")
        mu.mem_write(self.stop, b"\x00\xbf" * 8)
        mu.mem_write(self.api, (1).to_bytes(2, "little") + (4).to_bytes(2, "little")
                     + b"".join((self.stubs + 16 * i | 1).to_bytes(4, "little")
                                for i in range(len(self.names))))
        mu.hook_add(unicorn.UC_HOOK_CODE, self.on_stub, begin=self.stubs,
                    end=self.stubs + 16 * len(self.names))
        self.scr = ram + 0x40000                    # the API's scratch RAM (4 KB)
        for page in (0x48000000, 0x40021000, 0x40014000, 0x40020000, 0x40002000):
            mu.mmio_map(page, 0x1000, self.mmio_r, page, self.mmio_w, page)
        mu.reg_write(A.UC_ARM_REG_R0, self.api)
        mu.reg_write(A.UC_ARM_REG_SP, self.sp0)
        mu.reg_write(A.UC_ARM_REG_LR, self.stop | 1)
        self.pc = self.base + entry | 1

    def sample_now(self):
        if self.wob is None:
            return int(self.cyc / FCPU * self.fdma)
        return int(self.spos)

    def tick(self, cyc, poll=False):
        """the clock on by cyc cycles, the DMA on at its rate for what the CPU did"""
        self.cyc += cyc
        if self.wob is not None:
            self.spos += cyc / FCPU * self.fdma * (1 + self.wob * poll)
            self.bp.append((self.cyc, self.spos))

    def ib_at(self, j0, k):
        """the k samples the DMA reads from its j0-th on"""
        if self.wob is None:
            seg = self.ib[j0:j0 + k] if j0 < len(self.ib) else np.zeros(0, np.uint8)
            return np.concatenate([seg, np.zeros(k - len(seg), np.uint8)])
        c, sp = np.array(self.bp).T
        t = np.interp(j0 + np.arange(k) + 0.5, sp, c) / FCPU
        idx = (t * self.fhi).astype(np.int64)
        out = np.zeros(k, np.uint8)
        ok = idx < len(self.ib)
        out[ok] = self.ib[idx[ok]]
        return out

    def rssi(self):
        n = len(self.ib) if self.wob is None else int(len(self.ib) / self.fhi * self.fdma)
        on = self.on_at <= self.sample_now() < n
        return -60 if on else -125

    # ---- the peripherals ----
    def dma_fill(self):
        """the DMA's bytes up to now into the ring"""
        n = self.sample_now() - self.dma_s0
        rsz, cmar = self.reg.get(0x4002000C, 1024), self.reg.get(0x40020014, 0)
        if n > self.made + rsz:                     # more than a lap: only the last one shows
            self.made = n - rsz
        while self.made < n:
            k = min(n - self.made, rsz - self.made % rsz)
            seg = self.ib_at(self.dma_s0 + self.made, k)
            self.mu.mem_write(cmar + self.made % rsz, seg.tobytes())
            self.made += k
        if self.wob is not None:
            self.bp = self.bp[-1:]

    def mmio_r(self, uc, off, size, page):
        a = page + off
        if a == 0x4002000C:                          # DMA_CNDTR1
            self.nread += 1
            if not self.dma_on:
                return self.reg.get(a, 0)
            self.dma_fill()
            rsz = self.reg.get(0x4002000C, 1024)
            return rsz - self.made % rsz
        if a == 0x40014824:                          # TIM17_CNT: 96 MHz / (PSC + 1)
            return int(self.cyc / (self.reg.get(0x40014828, 0) + 1)) & 0xFFFF
        if a == 0x40002024:                          # TIM14_CNT: Aura's 100 us uptime
            return int(self.cyc / 9600) & 0xFFFF
        if a == 0x48000400:                          # GPIOB_MODER: PB1 an input
            return 0
        return self.reg.get(a, 0)

    def mmio_w(self, uc, off, size, value, page):
        a = page + off
        if a == 0x40020008:                          # DMA_CCR1
            if value & 1 and not self.dma_on:
                self.dma_on, self.dma_s0, self.made = True, self.sample_now(), 0
            elif not value & 1:
                self.dma_on = False
        if a == 0x4002000C and self.dma_on:
            return                                   # CNDTR is read-only while enabled
        self.reg[a] = value

    # ---- the API ----
    def on_stub(self, uc, addr, size, _):
        A = self.A
        name = self.names[(addr - self.stubs) // 16]
        r0, r1 = uc.reg_read(A.UC_ARM_REG_R0), uc.reg_read(A.UC_ARM_REG_R1)
        ret, us = 0, 1
        if name == "say":
            self.said.append(bytes(uc.mem_read(r0, 128)).split(b"\0")[0].decode())
            us = 5000
        elif name == "ticks":
            ret = int(self.cyc / 9600) & 0xFFFF
        elif name == "fd_read":
            us = 20
            ret = (self.rssi() + 174) << 1 if r0 == 0x67 else 0
        elif name == "fd_write":
            us = 20
            self.writes.setdefault(r0, []).append(r1)
        elif name == "delay_us":
            us = r0
        elif name == "ms":
            ret = int(self.cyc / 96000)
        elif name == "key_down":
            us = self.key_us                         # Aura scans six keypad rows, 5 us waits each
            self.keys += 1
            if self.stall and self.keys % self.stall[0] == 0:
                us = self.stall[1]                   # now and then much longer (an interrupt)
        elif name == "key_poll":
            us = 5                                   # all rows at once, the four columns read
            self.keys += 1
            if self.stall and self.keys % self.stall[0] == 0:
                us = self.stall[1]
        elif name in ("tx_on", "tx_pa", "cal_read", "cal_write"):
            ret = -1                                 # refused: receive-only tests
        elif name == "scratch":
            if r0:
                uc.mem_write(r0, (4096).to_bytes(4, "little"))
            ret = self.scr
        self.tick(us * 96)
        uc.reg_write(A.UC_ARM_REG_R0, ret & 0xFFFFFFFF)

    def run(self, prof=None):
        """-> (return value, stack bytes used), or None if it ran past max_s. prof: a Counter
        that gets the PC (an offset in the code) after each chunk; the chunks' lengths then vary,
        as a fixed one aliases with a module's per-count loop"""
        A = self.A
        pc = self.pc
        rng = np.random.default_rng(0)
        while self.cyc < self.max_cyc:
            n = int(rng.integers(CHUNK * 3 // 4, CHUNK * 5 // 4 + 1)) if prof is not None else CHUNK
            self.mu.emu_start(pc, self.stop, count=n)
            pc = self.mu.reg_read(A.UC_ARM_REG_PC)
            self.tick(n * self.cpi, self.nread >= 3)
            self.nread = 0
            if pc == self.stop:
                break
            if prof is not None:
                prof[pc - self.base] += 1
            pc |= 1
        else:
            return None
        st = bytes(self.mu.mem_read(self.stack_lo - 512, 3504 + 512))
        used = 3504 + 512 - next(i for i, b in enumerate(st) if b != 0xA5)
        return self.mu.reg_read(A.UC_ARM_REG_R0), used


def run_live(mode="4fsk", cn0=46.0, f_off=0.0, steps=10, seg_s=6.0, lead_s=1.5, cpi=1.5, seed=1,
             fdma=None, stall=None, src=None, wobble=None):
    """-> (return value, the module's lines, stack bytes used, the FD6818 writes). fdma: the
    DMA's rate (S/s; the module starts on its KS0 and must find it); stall (every, us): every
    so many key checks one takes that long (an interrupt, a slow scan)"""
    defs = []
    if steps != 10 or seg_s != 6.0 or lead_s != 1.5:
        defs = [("NG", steps), ("STEP_MS", f"{int(seg_s * 1000)}u"), ("LEAD_MS", f"{int(lead_s * 1000)}u")]
    code, entry = build(mode, defs, src)
    ib, on_at, fdma = stream(mode, cn0, f_off, steps, seg_s, lead_s, seed, fdma=fdma,
                             fine=wobble is not None)
    e = Emu(code, entry, ib, on_at, fdma, cpi, stall=stall, wobble=wobble)
    r = e.run()
    ret, used = r if r else (None, None)
    return ret, e.said, used, e.writes


IFPAGER = os.path.join(ROOT, "c", "k6", "mod", "ifpager_emu.c")


def run_ifpager(payloads, cn0=50.0, f_off=0.0, level=0.12, cpi=1.5, seed=1, fdma=None, stall=None,
                pre_s=0.3, after_s=0.5):
    """the pager's coded 4FSK session (c/k6/ifpager.c, in the test module ifpager_emu.c) against
    a modelled burst of these XL payloads (modem/k6g.py), the carrier already up when it starts
    (as after the control packet): -> (return value, its lines, stack bytes used, the packets
    it handed to sp_packet)"""
    from modem import k6g
    from sim.k6if import mcu, study as st
    code, entry = k6mod.build(IFPAGER)
    a = k6g.audio(k6g.burst_symbols(payloads), st.FSA, level)
    s = st.fm(st.ic7100(np.concatenate([np.zeros(int(pre_s * st.FSA)), a,
                                        np.zeros(int(after_s * st.FSA))])), f_off)
    fdma = fdma or mcu.FDMA
    bits = st.fe.receive(s, st.FZ, cn0, seed=seed, L=0, fdma=fdma)
    ib = bits.astype(np.uint8) * 2
    e = Emu(code, entry, ib, 0, fdma, cpi, stall=stall, max_s=len(ib) / fdma + 8.0)
    r = e.run()
    ret, used = r if r else (None, None)
    raw = bytes(e.mu.mem_read(e.scr + 1024, 4 + 128 * 64))
    n = int.from_bytes(raw[:4], "little")
    pk = [raw[4 + 128 * i:4 + 128 * (i + 1)] for i in range(min(n, 64))]
    return ret, e.said, used, pk


def symbols(src):
    """the module's functions: [(offset, size, name)], from the same build k6mod makes"""
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        obj, elf = os.path.join(d, "m.o"), os.path.join(d, "m.elf")
        flags = [("-O" + k6mod.source_opt(src)) if f == "-Os" else f for f in k6mod.CFLAGS]
        subprocess.run(["clang", *flags, "-c", src, "-o", obj], check=True)
        subprocess.run(["ld.lld", "-T", k6mod.LINK, "--defsym", "MODULE_BASE=0", "--gc-sections",
                        "--no-undefined", obj, "-o", elf], check=True)
        out = subprocess.run(["llvm-nm", "-S", elf], check=True, capture_output=True, text=True).stdout
    return [(int(p[0], 16), int(p[1], 16), p[3]) for p in map(str.split, out.splitlines())
            if len(p) == 4 and p[2] in "tT"]


def run_module(path, cpi=1.5, seconds=14.0, profile=False):
    """any level-2 GP0 module on the same stream (a strong 4FSK carrier after 2 s of nothing)
    -> (return value, its lines), and with profile a third: {function: share of the time}"""
    import collections
    code, entry = k6mod.build(path)
    ib, on_at, fdma = stream("4fsk", 60.0, 0.0, 2, seconds / 2, 1.5, 1, pre_s=2.0)
    e = Emu(code, entry, ib, on_at, fdma, cpi)
    prof = collections.Counter() if profile else None
    r = e.run(prof)
    shares = None
    if profile:
        syms, tot = symbols(path), max(sum(prof.values()), 1)
        shares = collections.Counter()
        for off, n in prof.items():
            shares[next((nm for a, sz, nm in syms if a <= off < a + sz), "(API)")] += n / tot
    ret = r[0] if r else None
    return (ret, e.said, shares) if profile else (ret, e.said)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", nargs="?", default="4fsk", choices=["4fsk", "ffsk"])
    ap.add_argument("--cn0", type=float, default=46.0)
    ap.add_argument("--off", type=float, default=0.0)
    ap.add_argument("--steps", type=int, default=10)
    ap.add_argument("--seg", type=float, default=6.0)
    ap.add_argument("--lead", type=float, default=1.5)
    ap.add_argument("--cpi", type=float, default=1.5)
    ap.add_argument("--fdma", type=float, help="the DMA's rate, S/s (default the model's 5.49e6)")
    ap.add_argument("--stall", help="EVERY,US: every so many key checks one takes US microseconds")
    ap.add_argument("--wobble", type=float, help="the DMA this much faster (a fraction) while "
                    "the CPU polls than while it computes (the sigma-delta's bits then at its clock)")
    ap.add_argument("--src", help="a variant of the mode's live module (e.g. c/k6/mod/iflive_tsweep.c)")
    ap.add_argument("--module", help="run this module's source instead (e.g. c/k6/mod/gp0dma.c)")
    ap.add_argument("--profile", action="store_true", help="with --module: the time in each function "
                    "(a module's polling loop counts as its own function's time)")
    a = ap.parse_args()
    if not shutil.which("clang"):
        sys.exit("needs clang and ld.lld")
    if a.module:
        ret, said, *rest = run_module(a.module, a.cpi, profile=a.profile)
        shares = rest[0] if rest else None
        for ln in said:
            print("K6 MOD say", ln)
        print(f"ret={ret}")
        for nm, x in shares.most_common() if shares else ():
            if x >= 0.001:
                print(f"{nm:16s} {100 * x:5.1f}%")
        return
    stall = tuple(int(x) for x in a.stall.split(",")) if a.stall else None
    ret, said, used, _ = run_live(a.mode, a.cn0, a.off, a.steps, a.seg, a.lead, a.cpi,
                                  fdma=a.fdma, stall=stall, src=a.src, wobble=a.wobble)
    for ln in said:
        print("K6 MOD say", ln)
    print(f"ret={ret} stack={used} B")


if __name__ == "__main__":
    main()
