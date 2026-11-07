"""The K6's own IF receiver, as it would run on the MCU (docs/k6-if-modem.md, c/k6/ifrx.c):
the study's IF matched filter rebuilt from what the radio has, checked against the study.

  counts (2 * ones - 64 at the DMA's real rate, ~85.8 kHz)
    -> complex decimating filter, 4:1, NT taps (a low-pass turned to the IF), computed only
       every 4th count                                       -> ~21.45 kHz, the IF's alias
    -> derotation (an NCO at the alias plus the carrier offset found on the lead) -> baseband
    -> per symbol (17.87 samples at the real rate, not the study's 18): |segment . template*|
       for each tone and audio start phase, max over phases; 18-sample templates made from the
       model (no training on the radio)
    -> timing: early-late on the winning template (one correlation either side), a
       first-order loop on a fractional symbol clock; the lead's best offset to start
    -> bits and max-log soft values

  python3 -m sim.k6if.mcu [ffsk1200 4fsk1200] [--off HZ]     BER vs C/N0 against the study
  python3 tools/k6_ifcap.py tables LOG... > c/k6/ifrx_tables.h   the C receiver's tables (the
                                                              model's, and 4FSK from captures)
MIT licence."""
import argparse

import numpy as np
from scipy import signal

from . import modes as md
from . import study as st

FDMA = 5.49e6               # the DMA's real rate (run 8); the study used 5.5296 for an exact 18
FC = FDMA / 64              # counts a second
D = 4
FS = FC / D                 # ~21.45 kHz
NT = 24                     # decimating filter taps
CUT = 7000.0                # its edge
TL = 18                     # template length (samples) at 1200 baud


def tl_of(baud):
    """samples a symbol: 18 at 1200 baud, 12 faster (about 4 counts apart either way; the
    acquisition's sweep needs TL a multiple of 6)"""
    return TL if baud <= 1200 else 12


def alias():
    """where the IF lands after 4:1 (Hz, -FS/2..FS/2)"""
    a = st.fe.FIF % FS
    return a - FS if a > FS / 2 else a


def taps(nt=NT, cut=CUT, fc=FC):
    """the complex decimating filter: a low-pass turned up to +FIF, so the -FIF image goes"""
    h = signal.firwin(nt, cut, fs=fc, window=("kaiser", 5.0))
    k = np.arange(nt)
    return h * np.exp(2j * np.pi * st.fe.FIF * k / fc)


def front(c, g, f_off=0.0):
    """counts -> the filter's complex output at every count, turned down by the IF and the
    carrier offset f_off: z[n] = e^(-j w n) sum g[k] c[n-k]. The MCU works this out only at
    the counts it samples (TL + 2 a symbol), so its output rate is locked to the symbols."""
    y = signal.lfilter(g, 1, c.astype(float))
    n = np.arange(len(y))
    return y * np.exp(-2j * np.pi * (st.fe.FIF + f_off) * n / FC)


def grid(start, pc, i, tl=TL):
    """the counts sampled for symbol positions i (0..TL-1, -1 and TL for early/late), a symbol
    starting at count `start` (float), pc counts a symbol"""
    return np.floor(start + np.asarray(i) * pc / tl + 0.5).astype(int)


class Link:
    """one mode over the model at the DMA's real rate: lead (alternating outer tones) + random
    symbols, the counts for any C/N0, and the true symbol clock"""

    def __init__(self, mode, nsym, seed=7, lead=64, floor=0.05, f_off=0.0, fdma=FDMA, pre=0.02):
        self.m, self.floor, self.fdma = mode, floor, fdma
        rng = np.random.default_rng(seed)
        self.syms = rng.integers(0, mode.M, nsym)
        lead_syms = np.tile([0, mode.M - 1], lead // 2)
        allsym = np.concatenate([lead_syms, self.syms, lead_syms[:16]])
        a, cls = mode.audio(allsym)
        self.cls = cls[lead:lead + nsym]
        self.allsym, self.allcls = allsym, cls
        self.lead = lead
        npre = int(pre * st.FSA)                 # bare carrier first (paplay's start)
        self.dev = st.ic7100(np.concatenate([np.zeros(npre), a]))
        self.s = st.fm(self.dev, f_off)
        self.t0 = npre / st.FSA                  # the lead's first symbol, s

    def counts(self, cn0, seed=1):
        return st.fe.receive(self.s, st.FZ, cn0, seed=seed, fdma=self.fdma, floor=self.floor)

    def sym_time(self, k):
        """symbol k of the data (k < 0: the lead) starts at this time, s"""
        return self.t0 + (self.lead + k) / self.m.baud


def _templates(lk, z, starts, pc, classes, tl=TL):
    seg = z[grid(starts[:, None], pc, np.arange(tl)[None, :], tl)]
    seg = seg / (seg[:, :1] / (np.abs(seg[:, :1]) + 1e-12))
    T = np.zeros((lk.m.M, len(classes), tl), complex)
    for i in range(lk.m.M):
        for j, c in enumerate(classes):
            sel = (lk.syms == i) & (lk.cls == c)
            T[i, j] = seg[sel].mean(0) if sel.any() else 0
    return T, seg


def make_templates(mode, nsym=3000, seed=11, tl=None):
    """templates (M, P, TL): each tone at each audio start phase present, averaged over a clean
    capture, each segment turned so its first sample has phase 0. P = the distinct start
    phases (2 for FFSK, 4 for these 4FSK tones). The segments start where the symbols arrive:
    the sender's filters delay them, so the offset (counts, after the filter's own delay) is
    the one whose templates tell the symbols apart best. Returns the templates, the classes and
    that offset (the receiver's timing is relative to it)."""
    tl = tl or tl_of(mode.baud)
    lk = Link(mode, nsym, seed=seed)
    z = front(lk.counts(None), taps())
    pc = FC / mode.baud
    base = lk.sym_time(np.arange(nsym)) * FC + (NT - 1) / 2
    classes = sorted(set(lk.cls.tolist()))
    best = None
    for o in range(-8, int(2 * pc), 2):
        T, seg = _templates(lk, z, base + o, pc, classes, tl)
        m = np.abs(seg @ np.conj(T.reshape(-1, tl)).T).reshape(nsym, mode.M, -1).max(2)
        srt = np.sort(m, 1)
        score = np.mean(srt[:, -1] - srt[:, -2]) * (np.mean(m.argmax(1) == lk.syms))
        if best is None or score > best[0]:
            best = (score, o, T)
    return best[2], classes, best[1]


class Rx:
    """the MCU's receiver in float: lead search, timing loop, decision-directed AFC, metrics"""

    def __init__(self, mode, T, gain=1 / 64, acq=None, afc=1 / 32, pull=(0, 1 / 8)):
        self.m, self.T, self.gain, self.afc = mode, T, gain, afc
        self.pull = pull                         # (symbols, gain): a faster loop at the start
        self.tl = T.shape[2]
        self.Tf = T.reshape(-1, self.tl)         # (M*P, TL)
        self.P = T.shape[1]
        self.acq = acq

    def run(self, z, t_first, nsym, pc=None):
        """z: front()'s output. t_first: a guess at the first symbol's start (counts; may be
        off by up to half a symbol when the lead's search is on). Returns metrics (nsym, M),
        the timing the loop ended on and the offset the AFC found (Hz)."""
        pc = FC / self.m.baud if pc is None else pc
        tau, TL = t_first, self.tl
        if self.acq is not None:
            # the lead: the best of 2 TL offsets (half-samples) by the mean winning metric
            best = None
            for o in np.arange(-TL, TL) * pc / TL / 2:
                s = tau + o + (np.arange(self.acq) - self.acq) * pc
                seg = z[grid(s[:, None], pc, np.arange(TL)[None, :], TL)]
                v = np.abs(seg @ np.conj(self.Tf).T).max(1).mean()
                if best is None or v > best[0]:
                    best = (v, o)
            tau += best[1]
        out = np.zeros((nsym, self.m.M))
        idx = np.arange(-1, TL + 1)
        h = TL // 2
        f = 0.0
        for k in range(nsym):
            n = grid(tau, pc, idx, TL)
            w = z[n] * np.exp(-2j * np.pi * f * n / FC)
            seg = w[1:-1]
            c = np.abs(self.Tf @ np.conj(seg))       # |seg . t*|, all templates
            out[k] = c.reshape(self.m.M, self.P).max(1)
            j = int(np.argmax(c))
            t = self.Tf[j]
            e = abs(np.dot(w[2:], np.conj(t))) - abs(np.dot(w[:-2], np.conj(t)))
            tau += pc + (self.pull[1] if k < self.pull[0] else self.gain) * pc * e / (c[j] + 1e-12)
            if self.afc:
                # the winning template's two halves: their phase difference is the offset's
                # turn over half a symbol
                c1 = np.dot(seg[:h], np.conj(t[:h]))
                c2 = np.dot(seg[h:], np.conj(t[h:]))
                f += self.afc * np.angle(c2 * np.conj(c1)) * FC / (np.pi * pc)
        return out, tau, f


def ber(mode_name, cn0s, nsym=4000, f_off=0.0, floor=0.05, nt=NT):
    tones, baud, lv = md.MODES[mode_name]
    mode = md.Mode(tones, baud, lv)
    T, _, o = make_templates(mode)
    lk = Link(mode, nsym, f_off=f_off, floor=floor)
    g = taps(nt)
    rx = Rx(mode, T, acq=32)
    # where the receiver thinks the data starts: the lead's start found by the carrier rising
    # is good to a few ms, so start the guess off by a third of a symbol
    t_true = lk.sym_time(0) * FC + (nt - 1) / 2 + o
    out = []
    for cn0 in cn0s:
        c = lk.counts(cn0, seed=int(cn0 * 10))
        z = front(c, g)
        met, _, _ = rx.run(z, t_true + 0.33 * FC / baud, nsym)
        sym = met.argmax(1)
        b = mode.bits_of
        out.append(np.mean(b[sym].ravel() != b[lk.syms].ravel()))
    return out


def c_tables(t4_k6=None, t4_k6_note=""):
    """c/k6/ifrx_tables.h: the decimating filter (Q14) and each mode's templates (4-bit re and
    im, -7..7, one scale for all of a mode's: the quantising costs about 0.02 dB), made from
    the model. t4_k6: 4FSK templates (M, P, TL) trained on the K6's own captures
    (tools/k6_ifcap.py tables), written as the table a build with IFRX_T4_K6 uses instead"""
    g = taps() * 16384
    g = np.round(g.real).astype(int) + 1j * np.round(g.imag).astype(int)
    head = (["/* ifrx_tables.h: made by `python3 tools/k6_ifcap.py tables LOG...` (sim/k6if/mcu.py) from the",
             "   K6 GP0 model: the decimating filter and the IF templates, and from gp0cap's captures: a",
             "   4FSK template set for builds with IFRX_T4_K6. SPDX-License-Identifier: MIT */"]
            if t4_k6 is not None else
            ["/* ifrx_tables.h: made by `python3 -m sim.k6if.mcu --tables` (sim/k6if/mcu.py) from the",
             "   K6 GP0 model: the decimating filter and the IF templates. SPDX-License-Identifier: MIT */"])
    out = head + [
        "#ifndef IFRX_TABLES_H", "#define IFRX_TABLES_H", "#include <stdint.h>", "",
           f"/* {NT} taps, Q14: a {CUT:.0f} Hz low-pass turned up to the IF ({st.fe.FIF:.1f} Hz at"
           f" {FC:.0f} counts/s);",
           "   re in the low 16 bits, im in the high: one load a tap */",
           f"static const uint32_t ifrx_g[{NT}] = {{"]
    out += ["  " + " ".join(f"0x{(int(v.real) & 0xFFFF) | ((int(v.imag) & 0xFFFF) << 16):08x}u," for v in g[i:i + 6])
            for i in range(0, NT, 6)]
    out += ["};"]
    sets = [("ffsk1200", None), ("4fsk1200", None)]
    if t4_k6 is not None:
        sets.append(("4fsk1200", t4_k6))
    for name, Tk in sets:
        tones, baud, lv = md.MODES[name]
        mode = md.Mode(tones, baud, lv)
        T, classes, o = make_templates(mode)
        if Tk is not None:
            T = Tk
        # the second half of the start phases are the first half's conjugates: keep the first
        P = len(classes)
        T = T[:, :P // 2]
        classes = classes[:P // 2]
        q = np.round(T / np.max(np.abs(np.concatenate([T.real.ravel(), T.imag.ravel()]))) * 7)
        q = ((q.real.astype(int) & 15) << 4) | (q.imag.astype(int) & 15)  # a byte: re, im
        cname = "ifrx_t_" + name.replace("1200", "")
        only = "IFRX_FFSK" if mode.M == 2 else "IFRX_4FSK"
        if mode.M == 4 and t4_k6 is not None:
            # the model's 4FSK table unless IFRX_T4_K6, then the one trained on the K6
            guard = (f"#if (!defined(IFRX_ONLY) || IFRX_ONLY == {only}) && "
                     + ("defined(IFRX_T4_K6)" if Tk is not None else "!defined(IFRX_T4_K6)"))
        else:
            guard = f"#if !defined(IFRX_ONLY) || IFRX_ONLY == {only}"
        what = (f"trained on the K6's captures{t4_k6_note}" if Tk is not None else "")
        out += ["", guard,
                f"/* {mode.name}{', ' + what if what else ''}: [tone][audio start phase {classes} of 24, each also half a turn on as its "
                "conjugate][sample], re in the high 4 bits, im in the low, each -7..7 */",
                f"static const uint8_t {cname}[{mode.M}][{len(classes)}][{TL}] = {{"]
        for i in range(mode.M):
            out.append("  {")
            for j in range(len(classes)):
                out.append("    {" + ", ".join(f"0x{int(v):02x}" for v in q[i, j]) + "},")
            out.append("  },")
        out += ["};", "#endif"]
    out += ["", "#endif", ""]
    return "\n".join(out)


H_BAUDS = (1200, 1350, 1440, 1500, 1600)


def c_tables_4h():
    """c/k6/ifrx_tables_4h.h: the model's templates for the four-tones-on-a-half-baud-grid sets
    (modes.MODES "4h<baud>"), one a tone (every symbol starts on audio phase 0 or a half turn,
    the conjugate), one set a baud rate chosen by IFRX_BAUD in a build with IFRX_ONLY = IFRX_4H"""
    out = ["/* ifrx_tables_4h.h: made by `python3 -m sim.k6if.mcu --tables-4h` (sim/k6if/mcu.py) from the",
           "   K6 GP0 model: the IF templates of the 4FSK sets with four tones on a half-baud grid",
           "   (baud/2, baud, 3 baud/2, 2 baud), for builds with IFRX_ONLY == IFRX_4H, one set a",
           "   rate (IFRX_BAUD). SPDX-License-Identifier: MIT */",
           "#ifndef IFRX_TABLES_4H_H", "#define IFRX_TABLES_4H_H", "#include <stdint.h>"]
    for b in H_BAUDS:
        tones, baud, lv = md.MODES[f"4h{b}"]
        mode = md.Mode(tones, baud, lv)
        tl = tl_of(baud)
        T, classes, o = make_templates(mode, tl=tl)
        assert classes == [0, 12], classes
        T = T[:, :1]
        q = np.round(T / np.max(np.abs(np.concatenate([T.real.ravel(), T.imag.ravel()]))) * 7)
        q = ((q.real.astype(int) & 15) << 4) | (q.imag.astype(int) & 15)
        out += ["", f"#if IFRX_BAUD == {b}",
                f"/* {mode.name}: [tone][audio start phase 0, half a turn on as its conjugate][sample], re in the high",
                "   4 bits, im in the low, each -7..7 */",
                f"static const uint8_t ifrx_t_4h[4][1][{tl}] = {{"]
        for i in range(4):
            out.append("  {{" + ", ".join(f"0x{int(v):02x}" for v in q[i, 0]) + "}},")
        out += ["};", "#endif"]
    out += ["", "#endif", ""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tables", action="store_true", help="print c/k6/ifrx_tables.h")
    ap.add_argument("--tables-4h", action="store_true", help="print c/k6/ifrx_tables_4h.h")
    ap.add_argument("modes", nargs="*", default=["ffsk1200", "4fsk1200"])
    ap.add_argument("--off", type=float, default=0.0, help="carrier offset, Hz")
    ap.add_argument("--nt", type=int, default=NT)
    ap.add_argument("--cn0", type=float, nargs="*", default=[38, 40, 42, 44, 46, 48, 50])
    a = ap.parse_args()
    if a.tables:
        print(c_tables(), end="")
        return
    if a.tables_4h:
        print(c_tables_4h(), end="")
        return
    for name in a.modes:
        tones, baud, lv = md.MODES[name]
        run = md.Run(md.Mode(tones, baud, lv), 4000)
        ref = []
        for c in a.cn0:
            ref.append(run.ser_ber(run.met_if(run.rx(c, seed=int(c))))[1])
        mine = ber(name, a.cn0, f_off=a.off, nt=a.nt)
        print(f"{name}: C/N0  study-IF   MCU-IF")
        for c, r, m in zip(a.cn0, ref, mine):
            print(f"        {c:4.0f}  {r:8.4f} {m:8.4f}", flush=True)
        print(f"  1% BER at {md.threshold(a.cn0, ref):.1f} (study) and "
              f"{(md.threshold(a.cn0, mine) or float('nan')):.1f} dB-Hz (MCU)")


if __name__ == "__main__":
    main()
