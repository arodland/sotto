"""The iflive module (c/k6/mod/iflive.c) on an emulated Cortex-M0 (unicorn) against a modelled
GP0 stream: the sender's live audio through the study's model (sim/k6if: the IC-7100, FM, the
FD6818's filter and AGC, the code 13 sigma-delta at the DMA's rate), as the bytes DMA channel 1
would write into the module's ring, paced by the emulated CPU's time. tests/test_k6_ifrx.py
uses it; run on its own for the full 10-step run:

  python3 tests/k6_live_emu.py [4fsk|ffsk] [--cn0 46] [--off -164] [--steps 10] [--cpi 1.5]

Time: the module runs in chunks of CHUNK instructions, each CPI cycles of the 96 MHz clock (the
module runs from RAM, no wait states; the M0 takes 1 to 3 cycles an instruction: 1.5 is a
middling mix of loads, multiplies and branches); the API's calls cost what the radio's do
(a line 5 ms, an FD6818 read 20 us). The DMA and TIM17 read the clock at each register read.
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


def build(mode, defs=()):
    """the module's code and entry, with #defines (name, value) ahead of its source"""
    src = LIVE if mode == "4fsk" else LIVE_FFSK
    if not defs:
        return k6mod.build(src)
    with tempfile.TemporaryDirectory() as d:
        w = os.path.join(d, "w.c")
        with open(w, "w") as f:
            f.write("#define K6_MODULE_LEVEL 2\n#define K6_MODULE_OPT z\n")
            f.writelines(f"#define {k} {v}\n" for k, v in defs)
            f.write(f'#include "{src}"\n')
        return k6mod.build(w)


def stream(mode, cn0, f_off=0.0, steps=10, seg_s=6.0, lead_s=1.5, seed=1, pre_s=0.25):
    """(bytes the DMA reads from GPIOB's IDR at its rate, bit 1 the code 13 output; the
    sample at which the carrier comes up). pre_s of nothing, then carrier and the live audio."""
    import k6_ifcap as ic
    from sim.k6if import mcu, study as st
    m = next(mm for n, mm in ic.PASSES.values() if n == mode)
    a = ic.live_audio(m, steps, seg_s, lead_s)
    n0 = int(0.2 * st.FSA)                      # key_and_play: carrier 0.2 s before the audio
    s = st.fm(st.ic7100(np.concatenate([np.zeros(n0), a])), f_off)
    bits = st.fe.receive(s, st.FZ, cn0, seed=seed, L=0, fdma=mcu.FDMA)  # Jeff's K6's rate
    lead = int(pre_s * mcu.FDMA)
    rng = np.random.default_rng(seed + 1)
    noise = rng.integers(0, 2, lead)            # no carrier: the squelched IF's idle toggling
    return np.concatenate([noise, bits]).astype(np.uint8) * 2, lead, mcu.FDMA


class Emu:
    def __init__(self, code, entry, ib, on_at, fdma, cpi=1.5, max_s=None):
        import unicorn
        from unicorn import arm_const as A
        self.U, self.A = unicorn, A
        self.ib, self.on_at, self.fdma, self.cpi = ib, on_at, fdma, cpi
        self.max_cyc = (max_s or (len(ib) / fdma + 1.0)) * FCPU
        self.cyc = 0.0
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
                      "key_down", "scratch"]
        for i in range(len(self.names)):
            mu.mem_write(self.stubs + 16 * i, b"\x70\x47")
        mu.mem_write(self.stop, b"\x00\xbf" * 8)
        mu.mem_write(self.api, (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
                     + b"".join((self.stubs + 16 * i | 1).to_bytes(4, "little")
                                for i in range(len(self.names))))
        mu.hook_add(unicorn.UC_HOOK_CODE, self.on_stub, begin=self.stubs,
                    end=self.stubs + 16 * len(self.names))
        for page in (0x48000000, 0x40021000, 0x40014000, 0x40020000):
            mu.mmio_map(page, 0x1000, self.mmio_r, page, self.mmio_w, page)
        mu.reg_write(A.UC_ARM_REG_R0, self.api)
        mu.reg_write(A.UC_ARM_REG_SP, self.sp0)
        mu.reg_write(A.UC_ARM_REG_LR, self.stop | 1)
        self.pc = self.base + entry | 1

    def sample_now(self):
        return int(self.cyc / FCPU * self.fdma)

    def rssi(self):
        on = self.on_at <= self.sample_now() < len(self.ib)
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
            j = self.dma_s0 + self.made
            seg = self.ib[j:j + k] if j < len(self.ib) else np.zeros(0, np.uint8)
            seg = np.concatenate([seg, np.zeros(k - len(seg), np.uint8)])
            self.mu.mem_write(cmar + self.made % rsz, seg.tobytes())
            self.made += k

    def mmio_r(self, uc, off, size, page):
        a = page + off
        if a == 0x4002000C:                          # DMA_CNDTR1
            if not self.dma_on:
                return self.reg.get(a, 0)
            self.dma_fill()
            rsz = self.reg.get(0x4002000C, 1024)
            return rsz - self.made % rsz
        if a == 0x40014824:                          # TIM17_CNT at 96 MHz
            return int(self.cyc) & 0xFFFF
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
            self.said.append(bytes(uc.mem_read(r0, 72)).split(b"\0")[0].decode())
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
        self.cyc += us * 96
        uc.reg_write(A.UC_ARM_REG_R0, ret & 0xFFFFFFFF)

    def run(self):
        A = self.A
        pc = self.pc
        while self.cyc < self.max_cyc:
            self.mu.emu_start(pc, self.stop, count=CHUNK)
            pc = self.mu.reg_read(A.UC_ARM_REG_PC)
            self.cyc += CHUNK * self.cpi
            if pc == self.stop:
                break
            pc |= 1
        else:
            return None
        st = bytes(self.mu.mem_read(self.stack_lo - 512, 3504 + 512))
        used = 3504 + 512 - next(i for i, b in enumerate(st) if b != 0xA5)
        return self.mu.reg_read(A.UC_ARM_REG_R0), used


def run_live(mode="4fsk", cn0=46.0, f_off=0.0, steps=10, seg_s=6.0, lead_s=1.5, cpi=1.5, seed=1):
    """-> (return value, the module's lines, stack bytes used, the FD6818 writes)"""
    defs = []
    if steps != 10 or seg_s != 6.0 or lead_s != 1.5:
        defs = [("NG", steps), ("STEP_MS", f"{int(seg_s * 1000)}u"), ("LEAD_MS", f"{int(lead_s * 1000)}u")]
    code, entry = build(mode, defs)
    ib, on_at, fdma = stream(mode, cn0, f_off, steps, seg_s, lead_s, seed)
    e = Emu(code, entry, ib, on_at, fdma, cpi)
    r = e.run()
    ret, used = r if r else (None, None)
    return ret, e.said, used, e.writes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", nargs="?", default="4fsk", choices=["4fsk", "ffsk"])
    ap.add_argument("--cn0", type=float, default=46.0)
    ap.add_argument("--off", type=float, default=0.0)
    ap.add_argument("--steps", type=int, default=10)
    ap.add_argument("--seg", type=float, default=6.0)
    ap.add_argument("--lead", type=float, default=1.5)
    ap.add_argument("--cpi", type=float, default=1.5)
    a = ap.parse_args()
    if not shutil.which("clang"):
        sys.exit("needs clang and ld.lld")
    ret, said, used, _ = run_live(a.mode, a.cn0, a.off, a.steps, a.seg, a.lead, a.cpi)
    for ln in said:
        print("K6 MOD say", ln)
    print(f"ret={ret} stack={used} B")


if __name__ == "__main__":
    main()
