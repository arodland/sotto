"""DMR mute inside EXPLORE's Listen for ID (c/k6/xdmr.c, docs/k6-dmr.md): xlisten's steps with the
DMR detector draining the GP0 DMA ring in their waits, on the emulated Cortex-M0 against the
modelled stream (tests/k6_live_emu.py). A repeater's DMR must be found within a step or two and
mute the speaker, FM voice must open it after the hold, the ring must keep up while the tone
detector is polled every 2 ms, and the stack (the 2 KB ring on it) must stay in budget.
Needs clang, ld.lld and unicorn. SPDX-License-Identifier: MIT"""
import os
import re
import shutil
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modem import k6mod  # noqa: E402
from sim.k6if import dmr, frontend as fe, study as st  # noqa: E402

pytestmark = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")

AF_FM = 0x6140
# a listen as EXPLORE runs it: xl_start, xd_start, steps until the time is up, xd_stop, xl_stop
HARNESS = r"""
#define K6_MODULE_LEVEL 2
#include "cwid.c"
#include "xlisten.c"
#include "xdmr.c"
int k6_main(const k6_api_t *api) {
  xl_t x;
  xd_t d;
  int r;
  xl_start(&x, api, 440925000u, RUN_S);
  xd_start(&d, api, 1, 0);
  while ((r = xd_step(&d, &x)) != XL_TIME && r != XL_OK) {}
  r = xd_stop(&d);
  xl_stop(&x, 2);
  return r;
}
"""


def build(run_s):
    with tempfile.TemporaryDirectory() as d:
        w = os.path.join(d, "w.c")
        with open(w, "w") as f:
            f.write(f"#define RUN_S {run_s}u\n#include <stdint.h>\n")
            f.write(HARNESS.replace('#include "', f'#include "{K6}/'))
        return k6mod.build(w)


def stream(s, cn0, pre_s, post_s, seed=1):
    bits = fe.receive(s, st.FZ, cn0, seed=seed, L=0, fdma=fe.FDMA)
    rng = np.random.default_rng(seed + 1)
    a, b = int(pre_s * fe.FDMA), int(post_s * fe.FDMA)
    ib = np.concatenate([rng.integers(0, 2, a), bits, rng.integers(0, 2, b)]).astype(np.uint8) * 2
    return ib, a, a + len(bits)


def listen(s, run_s=3, cn0=60.0, pre_s=0.0, post_s=0.5, level=-85, tone=0):
    """-> (DMR transmissions, say lines (ms, text), REG_47 writes (ms, value), stack bytes)"""
    import k6_live_emu as le

    class Emu(le.Emu):
        def on_stub(self, uc, addr, size, _):
            name, r0 = self.names[(addr - self.stubs) // 16], uc.reg_read(self.A.UC_ARM_REG_R0)
            # REG_47 reads as monitor_on left it (RX audio on the speaker)
            if name == "fd_read" and r0 in (0x47, 0x68):
                self.tick(20 * 96)
                # REG_68: the sub-audio scan's CTCSS word for `tone` (0.1 Hz) while the carrier is up
                v = AF_FM if r0 == 0x47 else \
                    tone * 206489 // 100000 + 1 if tone and on <= self.sample_now() < off else 0x8000
                uc.reg_write(self.A.UC_ARM_REG_R0, v)
                return
            if name == "fd_write" and r0 == 0x47:
                w47.append((round(self.cyc / 96000), uc.reg_read(self.A.UC_ARM_REG_R1)))
            if name == "say":
                at.append(round(self.cyc / 96000))
            return super().on_stub(uc, addr, size, _)

    w47, at = [], []

    code, entry = build(run_s)
    ib, on, off = stream(s, cn0, pre_s, post_s)
    e = Emu(code, entry, ib, on, fe.FDMA, 1.5, max_s=run_s + 1.0)
    e.rssi = lambda: level if on <= e.sample_now() < off else -120
    # paint 8 KB under the stack: the 2 KB ring and the listener's state are on it
    lo = e.sp0 - 8192
    e.mu.mem_write(lo, b"\xa5" * 8192)
    r = e.run()
    assert r is not None, "\n".join(e.said)
    stk = bytes(e.mu.mem_read(lo, 8192))
    used = 8192 - next(i for i, b in enumerate(stk) if b != 0xA5)
    return r[0], list(zip(at, e.said)), w47, used


def line(said, head):
    return next(((t, ln) for t, ln in said if ln.startswith(head)), (None, None))


SHUT, OPEN = AF_FM & ~0x0F00, AF_FM


def test_listen_mutes_a_repeaters_dmr():
    pytest.importorskip("unicorn")
    sy, _ = dmr.bs_symbols(2.0, ("voice", "idle"), seed=7)
    s, _ = dmr.dmr_baseband(sy, pre_s=0.0, post_s=0.0)
    n, said, w47, stack = listen(s, run_s=3)
    txt = "\n".join(ln for _, ln in said)
    print(txt, w47, stack)
    assert n == 1, txt
    _, on = line(said, "DMR on bs")
    assert int(on.split("+")[1]) <= 150, on          # inside the hold: the speaker never opened
    t_off, _ = line(said, "DMR off")
    assert t_off is not None, txt
    # shut from the start; opened only once DMR was over (the carrier ended at 2 s)
    assert w47[0][1] == SHUT and all(v == SHUT or t >= t_off for t, v in w47), (w47, t_off)
    _, end = line(said, "DMR end")
    assert int(re.search(r"lost=(\d+)", end)[1]) <= 3, end
    ks = int(re.search(r"ks=(\d+)", end)[1])
    assert abs(ks - fe.FDMA / 1000) < 60, end
    assert stack < 4096, stack


def test_listen_opens_for_fm_voice():
    pytest.importorskip("unicorn")
    a = dmr.speech("female_libri198_14s__original", 2.0)
    n, said, w47, _ = listen(dmr.fm_voice(a, ctcss=100.0), run_s=2, tone=1000)
    txt = "\n".join(ln for _, ln in said)
    assert n == 0, txt
    assert line(said, "CTCSS 1000")[1] is not None, txt     # two readings in a row: the tone
    assert "ctcss=1000/" in line(said, "DMR end")[1], txt
    assert line(said, "DMR on")[1] is None, txt
    # shut for the hold, then open about 150 ms in and staying so
    assert w47[0][1] == SHUT and w47[1][1] == OPEN, w47
    assert w47[1][0] - w47[0][0] < 300 and all(v == OPEN for _, v in w47[1:]), w47


def test_a_late_dmr_key_up_mutes_an_open_speaker():
    """the repeater keying up DMR while the speaker is open (FM, or silence, before it)"""
    pytest.importorskip("unicorn")
    sy, _ = dmr.bs_symbols(1.0, ("idle", "idle"), seed=3)
    s, _ = dmr.dmr_baseband(sy, pre_s=0.0, post_s=0.0)
    n, said, w47, _ = listen(s, run_s=3, pre_s=1.0, post_s=0.5)
    txt = "\n".join(ln for _, ln in said)
    assert n == 1, txt
    _, on = line(said, "DMR on bs")
    # the carrier came up ~1.0 s into the stream: DMR within a step or two of it
    assert 1000 <= int(on.split("+")[1]) <= 1200, on
    t_open = next(t for t, v in w47 if v == OPEN)
    t_shut = next(t for t, v in w47 if v == SHUT and t > t_open)
    assert t_open < 400 and 1000 <= t_shut <= 1250, w47
