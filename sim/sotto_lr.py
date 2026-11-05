"""sotto 1200: 48 bits per 40 ms superframe, built on the v3.7 core (8 kHz).

Python reference for the low-rate variant meant for the Baofeng UV-K6 voice pager at FFSK
1800 baud: one 6-byte slot of a K6 XL packet carries 40 ms instead of 20 ms, so a 128-byte
packet holds 800 ms of speech, against about 690 ms on air at 1800 baud.

A superframe covers two 20 ms frames, A (first) and B (second). A is coded like a v3.7 frame
with the shape's last two stages dropped; B is coded as a small correction to the halfway
point between this superframe's A and the next one's:

    bits  0-1   voicing A      v3.2 code (UU / VV / MM / edge), as v3.7
    bits  2-8   Wo A           7 bits, as v3.7
    bits  9-13  energy A       5 bits, as v3.7 (code 0 = silence)
    bits 14-31  shape A        stages 1-2 of v3.7's 9+9+8+8 codebook (18 bits)
    bits 32-33  voicing B      v3.2 code
    bits 34-36  Wo B           3-bit step from the interpolated pitch index (WO_STEP)
    bits 37-39  energy B       3-bit step from the interpolated energy code (E_STEP)
    bits 40-47  shape B        the mean of the two anchors' shapes plus 1.5 x a stage-3
                               codeword (8 bits)

The encoder picks each A shape among its M-best paths counting the interpolated B frames on
both sides of it (JOINT), so it looks 2 superframes ahead.
The first 14 bits are laid out exactly like a v3.7 frame, so the K6's automatic gain (which
reads the energy code at bits 9-13) works unchanged, and the decoder needs no tables beyond
v3.7's. The decoder rebuilds per-20 ms parameters (voicing code, Wo, energy, shape) and runs
v3.7's synthesis on them: shape and energy are then linear between 10 ms points as in v3.7.
"""
import os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vocoder_lab as vl

# B's pitch: index steps from the interpolated Wo index (7-bit log scale, 1.8% per step)
WO_STEP = np.array([-12, -5, -2, -1, 0, 2, 5, 12])
# B voiced but neither anchor voiced (about 1% of voiced B frames): the code is an absolute index
WO_ABS = np.array([10, 23, 30, 36, 42, 53, 75, 103])
# B's energy: code steps (2.26 dB each) from the interpolated energy code
E_STEP = np.array([-6, -3, -2, -1, 0, 1, 2, 4])
E_ZERO = 4                                              # E_STEP[E_ZERO] == 0
WO_ZERO = 4


def _q_nearest(table, x):
    return int(np.argmin(np.abs(np.asarray(table) - x)))


class Sotto1200(vl.SottoV37):
    """Low-rate sotto: 48 bits / 40 ms. See the module docstring for the frame."""

    A_STAGES = (0, 1)             # stages of v3.7's 9+9+8+8 codebook sent for A's shape
    B_RES = (2, 1.5)              # (stage, gain): B's shape = interpolation + gain * that stage's codeword
    WO_A_BITS = 7
    B_WO_BITS = 3
    B_E_BITS = 3
    W_TAB, W_ABS, W_ZERO = WO_STEP, WO_ABS, WO_ZERO
    E_TAB, E_ZERO = E_STEP, E_ZERO
    BITS = 48
    JOINT = 1.0                   # weight of the neighbouring B frames in A's shape choice
    INTERP = True                 # rebuild a lost superframe between two good ones from both

    def wa_index(self, wo):
        return self.wo_index(wo) >> (7 - self.WO_A_BITS)

    def wa_to7(self, wa):
        sh = 7 - self.WO_A_BITS
        return (wa << sh) + ((1 << sh) >> 1)

    def __init__(self, vq=None, **kw):
        super().__init__(vq=vq, **kw)
        assert sum(w for _, w in self.layout()) == self.BITS, self.layout()
        self.lw0, self.lw1 = np.log(vl.WO_MIN), np.log(self.WO_HI)

    # ------------------------------------------------------------ analysis (v3.7's)
    def params(self, x):
        """Per 20 ms frame: voicing code, Wo, energy (dB), mean-removed shape (K)."""
        m = vl.analyse(x, self.PITCH)
        c = vl.voicing_classes(x, m)
        if len(m) % 2:
            m = np.concatenate([m, m[-1:]])
            c = np.concatenate([c, c[-1:]])
        rk = vl.to_rate_k(m)
        en = vl.frame_energy_db(vl.from_rate_k(m, rk))
        n = len(m) // 2
        code = np.zeros(n, int)
        for f in range(n):
            a, b = c[2 * f], c[2 * f + 1]
            code[f] = a if a == b else (3 if 0 in (a, b) else 1)
        wo = m["Wo"][0::2]
        sh = rk[0::2] - rk[0::2].mean(1, keepdims=True)
        # pitch and energy indices exactly as the v3.7 frame codes them (SottoV0.encode)
        wi = np.array([int(vl.q_wo(np.array([w]), hi=self.WO_HI)[0][0]) for w in wo])
        ei = np.array([int(vl.q_uniform(np.array([e]), self.E_LO, self.E_HI, 5)[0][0]) for e in en[0::2]])
        return dict(code=code, wo=wo.astype(float), en=en[0::2], shape=sh, rk=rk, wi=wi, ei=ei)

    def wo_index(self, wo):
        return int(np.clip(np.round((np.log(wo) - self.lw0) / (self.lw1 - self.lw0) * 127), 0, 127))

    def wo_value(self, wi):
        return float(np.exp(self.lw0 + wi * (self.lw1 - self.lw0) / 127))

    @staticmethod
    def e_index(e):
        return int(np.clip(np.round((e - vl.SottoV0.E_LO) / (vl.SottoV0.E_HI - vl.SottoV0.E_LO) * 31), 0, 31))

    # ------------------------------------------------------------ B from the anchors
    @staticmethod
    def wo_interp(wa, va, wb, vb):
        """Interpolated pitch index for B from the anchors' indices and voiced flags, or None
        when neither anchor is voiced."""
        if va and vb:
            return (wa + wb + 1) >> 1
        if va:
            return wa
        if vb:
            return wb
        return None

    @staticmethod
    def e_interp(ea, eb):
        return (ea + eb + 1) >> 1

    def e_b(self, ea, en, de):
        """B's energy code from the anchors' and its 3-bit step."""
        if ea == 0 and en == 0 and self.E_TAB[de] <= 0:
            return 0
        return int(np.clip(self.e_interp(ea, en) + self.E_TAB[de], 0, 31))

    def layout(self):
        """Bit layout: (name, width) in transmission order."""
        f = [("ca", 2), ("wa", self.WO_A_BITS), ("ea", 5)]
        f += [(f"sa{i}", self.vq.bits[st]) for i, st in enumerate(self.A_STAGES)]
        f += [("cb", 2), ("dw", self.B_WO_BITS), ("de", self.B_E_BITS)]
        if self.B_RES is not None:
            f += [("sb", self.vq.bits[self.B_RES[0]])]
        return f

    def a_shape(self, si):
        return sum(self.vq.cb[st][i] for st, i in zip(self.A_STAGES, si))   # DCT 1..K-1

    def b_shape(self, ca, cn, sb):
        base = (4 * ca + 4 * cn) / 8
        if self.B_RES is None:
            return base
        st, g = self.B_RES
        return base + g * self.vq.cb[st][sb]

    def _a_search(self, c, all_paths=False):
        """M-best over the A stages: index list and reconstruction for DCT target c (with
        all_paths, every surviving path, best first)."""
        paths, rec = [[]], np.zeros((1, len(c)))
        for st in self.A_STAGES:
            cb = self.vq.cb[st]
            res = c[None] - rec
            d = (res ** 2).sum(1)[:, None] - 2 * res @ cb.T + (cb ** 2).sum(1)[None]
            flat = np.argsort(d, axis=None)[:self.vq.m]
            pi, ci = flat // cb.shape[0], flat % cb.shape[0]
            paths = [paths[i] + [int(j)] for i, j in zip(pi, ci)]
            rec = rec[pi] + cb[ci]
        if all_paths:
            o = np.argsort(((c[None] - rec) ** 2).sum(1), kind="stable")
            return [paths[i] for i in o], rec[o]
        k = int(np.argmin(((c[None] - rec) ** 2).sum(1)))
        return paths[k], rec[k]

    def _b_err(self, base, cb_target):
        """Squared error of B's best residual codeword over interpolation base(s) (P, 19)."""
        st, g = self.B_RES
        r = cb_target[None] - base                                  # P, 19
        cbk = g * self.vq.cb[st]
        d = (r ** 2).sum(1)[:, None] - 2 * r @ cbk.T + (cbk ** 2).sum(1)[None]
        return d.min(1)

    def _joint(self, A, cc, ei):
        """Re-pick each anchor's shape path counting the B frames on both sides of it."""
        ns = len(A)
        wB = [1.0 if ei[2 * s + 1] > 0 else 0.1 for s in range(ns)]
        wA = [1.0 if A[s]["ea"] > 0 else 0.1 for s in range(ns)]
        for s in range(ns):
            paths, rec = self._a_search(cc[2 * s], all_paths=True)
            err = wA[s] * ((rec - cc[2 * s][None]) ** 2).sum(1)
            if s > 0:
                err = err + self.JOINT * wB[s - 1] * self._b_err(0.5 * (A[s - 1]["rec"][None] + rec), cc[2 * s - 1])
            nx = A[s + 1]["rec"][None] if s + 1 < ns else rec
            err = err + self.JOINT * wB[s] * self._b_err(0.5 * (rec + nx), cc[2 * s + 1])
            k = int(np.argmin(err))
            A[s]["si"], A[s]["rec"] = paths[k], rec[k]

    def encode_params(self, p):
        code, wi, ei, sh = p["code"], p["wi"], p["ei"], p["shape"]
        nf = len(code)
        if nf % 2:                                        # pad to whole superframes
            code, wi, ei = (np.concatenate([a, a[-1:]]) for a in (code, wi, ei))
            sh = np.concatenate([sh, sh[-1:]])
            nf += 1
        ns = nf // 2
        cc = (sh @ vl.DCT.T)[:, 1:]
        A = []
        for s in range(ns):
            si, rec = self._a_search(cc[2 * s])
            A.append(dict(ca=int(code[2 * s]), wa=int(wi[2 * s]) >> (7 - self.WO_A_BITS),
                          ea=int(ei[2 * s]), si=si, rec=rec))
        if self.JOINT and self.B_RES is not None:
            self._joint(A, cc, ei)
        out = []
        for s in range(ns):
            a, nxt = A[s], A[s + 1] if s + 1 < ns else A[s]
            v = dict(ca=a["ca"], wa=a["wa"], ea=a["ea"])
            for i, si in enumerate(a["si"]):
                v[f"sa{i}"] = si
            cb, wb, eb = int(code[2 * s + 1]), int(wi[2 * s + 1]), int(ei[2 * s + 1])
            v["cb"] = cb
            v["de"] = _q_nearest(self.E_TAB, eb - self.e_interp(a["ea"], nxt["ea"]))
            wint = self.wo_interp(self.wa_to7(a["wa"]), a["ca"] != 0, self.wa_to7(nxt["wa"]), nxt["ca"] != 0)
            if cb == 0:
                v["dw"] = self.W_ZERO                     # unvoiced: pitch unused
            elif wint is None:
                v["dw"] = _q_nearest(self.W_ABS, wb)
            else:
                v["dw"] = _q_nearest(self.W_TAB, wb - wint)
            if self.B_RES is not None:
                st, g = self.B_RES
                r = cc[2 * s + 1] - (4 * a["rec"] + 4 * nxt["rec"]) / 8
                v["sb"] = int(np.argmin(((r[None] - g * self.vq.cb[st]) ** 2).sum(1)))
            out.append(self._pack([(v[k], w) for k, w in self.layout()]))
        return np.array(out, np.int8)

    def encode(self, x):
        return self.encode_params(self.params(x))

    # ------------------------------------------------------------ decoder
    def unpack(self, b):
        b = np.asarray(b)
        f, p = {}, 0
        for k, w in self.layout():
            f[k] = int("".join(map(str, b[p:p + w])), 2)
            p += w
        f["si"] = [f[f"sa{i}"] for i in range(len(self.A_STAGES))]
        f["wa7"] = self.wa_to7(f["wa"])
        return f

    def _b_frame(self, a, n, cb, dw, de, sb):
        """A 20 ms frame halfway between frames a and n (code, wi, ei, c): B's rule, with B's
        voicing cb, pitch and energy steps dw, de and stage-3 residual sb (None: none)."""
        eb = self.e_b(a["ei"], n["ei"], de)
        wint = self.wo_interp(a["wi"], a["code"] != 0, n["wi"], n["code"] != 0)
        if wint is None:
            wb = int(self.W_ABS[dw]) if cb else a["wi"]
        elif cb == 0:
            wb = wint
        else:
            wb = int(np.clip(wint + self.W_TAB[dw], 0, 127))
        c = (4 * a["c"] + 4 * n["c"]) / 8 if sb is None else self.b_shape(a["c"], n["c"], sb)
        return dict(code=cb, wi=wb, ei=eb, c=c)

    def frame_params(self, bits, good=None):
        """Superframes -> per 20 ms frame (code, Wo index, energy code, shape in DCT 1..K-1).
        A lost superframe between two good ones (INTERP; the K6 plays from store, so the next
        one is known) is rebuilt from both by B's rule with no steps or residual: A halfway
        between the neighbours' A frames with the earlier one's voicing, then B halfway
        between that and the next A with its voicing. Other lost superframes repeat the last
        good one (concealment fades them). Sets self.rebuilt."""
        ns = len(bits)
        good = np.ones(ns, bool) if good is None else np.asarray(good, bool)
        F, last = [], None
        for s in range(ns):
            if good[s]:
                last = self.unpack(bits[s])
            F.append(last)
        sil = self.unpack(np.zeros(self.BITS, int))      # before the first good superframe
        sil.update(dw=self.W_ZERO, de=self.E_ZERO)
        F = [f if f is not None else sil for f in F]
        A = [dict(code=f["ca"], wi=f["wa7"], ei=f["ea"], c=self.a_shape(f["si"])) for f in F]
        self.rebuilt = np.zeros(ns, bool)
        if self.INTERP:
            for s in range(1, ns - 1):
                if not good[s] and good[s - 1] and good[s + 1]:
                    A[s] = self._b_frame(A[s - 1], A[s + 1], F[s - 1]["ca"], self.W_ZERO, self.E_ZERO, None)
                    self.rebuilt[s] = True
        avail = good | self.rebuilt
        out = []
        for s in range(ns):
            a, f = A[s], F[s]
            out.append(dict(a))
            if self.rebuilt[s]:
                out.append(self._b_frame(a, A[s + 1], F[s + 1]["ca"], self.W_ZERO, self.E_ZERO, None))
            else:
                nxt = A[s + 1] if s + 1 < ns and avail[s + 1] else a
                out.append(self._b_frame(a, nxt, f["cb"], f["dw"], f["de"], f.get("sb", 0)))
        for q in out:
            c = np.zeros(vl.K)
            c[1:] = q.pop("c")
            q["shape"] = c @ vl.DCT
        return out

    def decode(self, bits, good=None):
        fp = self.frame_params(bits, good)
        if good is not None:
            good = np.asarray(good, bool) | self.rebuilt
        return self._conceal(self._synth(fp), good)

    def _synth(self, fp):
        """Per 20 ms frame parameters -> MODEL per 10 ms, as v3.7's decoder."""
        n = len(fp)
        voiced = np.zeros(2 * n, int)
        wo = np.zeros(2 * n)
        rk = np.zeros((2 * n, vl.K))
        en = np.zeros(2 * n)
        for f, q in enumerate(fp):
            voiced[2 * f], voiced[2 * f + 1] = self._read_voicing(q["code"], voiced[2 * f - 1] if f else 0)
            wo[2 * f] = self.wo_value(q["wi"])
            en[2 * f] = -60.0 if q["ei"] == 0 else self.E_LO + q["ei"] * (self.E_HI - self.E_LO) / 31
            shape = q["shape"]
            if self.pf:
                shape = vl.postfilter_rk(shape[None], self.pf)[0]
            rk[2 * f] = shape
        rk = vl.decimate_interp(rk)
        en = vl.decimate_interp(en)
        wo[1::2] = wo[0::2]
        wo = vl.interp_wo(wo, voiced)
        wo = np.where(voiced > 0, wo, np.maximum(wo, vl.WO_MIN))
        m = np.zeros(2 * n, vl.MODEL)
        m = vl.set_wo(m, wo, np.minimum(voiced, 1))
        m = vl.set_energy(vl.from_rate_k(m, rk), en)
        for k in np.where(voiced == vl.MIXED)[0]:
            m[k]["voiced"] = vl.mixed_cutoff(m[k]["Wo"])
        return m

    def _conceal(self, m, good):
        """v3.5 concealment on 20 ms frames: a lost superframe is two lost frames (hold 40 ms,
        then 6 dB per 20 ms, silent after 80 ms)."""
        if good is None or np.all(good):
            return m
        run, r = [], 0
        for g in good:
            for _ in range(2):
                r = 0 if g else r + 1
                run.append(r)
        for f, j in enumerate(run):
            if j == 0:
                continue
            for k in (2 * f, 2 * f + 1):
                if k >= len(m):
                    continue
                g = 1e-4 if j > self.max_lost else \
                    10 ** (-self.slope * max(0, j - self.hold + (k - 2 * f) / 2) / 20)
                m[k]["A"] *= g
        return m

    def decode_audio(self, bits, good=None):
        return vl.synthesise(self.decode(bits, good))
