"""Very-low-rate sotto: 48 bits per superframe of N 20 ms frames, on the v3.7 decoder core.

    sotto 600   N = 4   48 bits / 80 ms
    sotto 400   N = 6   48 bits / 120 ms

Both keep sotto's 6-byte slot, so a K6 XL packet's 20 slots carry 1.6 s (600) or 2.4 s (400)
of speech. Each superframe codes its N frames jointly (bits, in transmission order):

                                   600   400
    voicing pattern                  3     3
    pitch line centre, slope       7+3   7+3
    energy mean, residual          5+5   5+5
    shape anchors (frame: stages)  0: 7+6, 2: 7+5    0: 9, 2: 8, 4: 8

So the shape is sent every 40 ms in both modes (sotto 400 with smaller codebooks), which the
oracle runs in docs/sotto-vlr.md show is what intelligibility needs. In detail:

    voicing   one entry of a trained table of N-frame voicing patterns (v3.2 codes per frame)
    pitch     a straight line through the voiced frames' log-Wo indices: its value at the
              superframe's centre (7 bits) and its slope (a trained table)
    energy    the mean of the N 5-bit energy codes (5 bits) plus a trained N-dimensional
              residual codebook (gain-shape)
    shape     one or more anchor frames, each coded with its own trained multistage
              codebook; the frames between anchors (and up to the next superframe's first
              anchor) are linear between them. The encoder picks every anchor's codeword by
              a Viterbi search over its M-best candidates, counting the error of the
              interpolated frames on both sides, so anchors are chosen for the trajectory
              and not only for themselves.

The decoder rebuilds per-20 ms parameters (voicing code, Wo index, energy code, shape) and
runs sotto 1200's v3.7 synthesis on them. It needs the next superframe's first anchor, so its
output is one superframe behind; with a store-and-forward link like the K6 pager that costs
nothing. Tables: sim/data/vlr_tables.npz, written by tools/train_vlr.py from Speech Commands
(CC BY 4.0).
"""
import os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vocoder_lab as vl
import sotto_lr as lr

TABLES = os.path.join(HERE, "data", "vlr_tables.npz")

# voicing: each 20 ms code as two 10 ms halves (U, V, M); edge (3) taken as (M, U)
HALVES = {0: (0, 0), 1: (1, 1), 2: (2, 2), 3: (2, 0)}
HALF_COST = np.array([[0, 1.0, 0.6], [1.0, 0, 0.3], [0.6, 0.3, 0]])
V_COST = np.array([[sum(HALF_COST[a, b] for a, b in zip(HALVES[t], HALVES[c])) for c in range(4)]
                   for t in range(4)])

MODES = {
    # name: N, voicing bits, pitch centre bits, slope bits, energy mean bits, energy residual
    # bits, shape anchors as (frame offset, stage bits)
    "600": dict(N=4, bv=3, bw=7, bs=3, be=5, br=5, anchors=((0, (7, 6)), (2, (7, 5)))),
    "400": dict(N=6, bv=3, bw=7, bs=3, be=5, br=5, anchors=((0, (9,)), (2, (8,)), (4, (8,)))),
}


def mode_bits(cfg):
    return cfg["bv"] + cfg["bw"] + cfg["bs"] + cfg["be"] + cfg["br"] + \
        sum(sum(b) for _, b in cfg["anchors"])


def frame_weight(ei):
    """Error weight of a frame by its energy code: near-silent frames count little."""
    return np.where(np.asarray(ei) >= 4, 1.0, 0.1)


class SottoVLR(lr.Sotto1200):
    """48 bits per N x 20 ms superframe; see the module docstring."""

    M_BEST = 16

    def __init__(self, mode="600", tables=None, cfg=None, **kw):
        self.mode = mode
        self.cfg = dict(cfg or MODES[mode])
        self.N = self.cfg["N"]
        self.BITS = mode_bits(self.cfg)
        super().__init__(**kw)
        t = tables if tables is not None else dict(np.load(TABLES))
        p = f"m{mode}_"
        self.vpat = np.asarray(t[p + "vpat"], int)                       # (2^bv, N)
        self.slopes = np.asarray(t[p + "slope"], float)                   # (2^bs,)
        self.eres = np.asarray(t[p + "eres"], float)                      # (2^br, N)
        self.scb = [[np.asarray(t[f"{p}a{j}s{i}"], float) for i in range(len(b))]
                    for j, (_, b) in enumerate(self.cfg["anchors"])]

    # ------------------------------------------------------------ layout
    def layout(self):
        c = self.cfg
        f = [("v", c["bv"]), ("wc", c["bw"]), ("ws", c["bs"]), ("em", c["be"]), ("er", c["br"])]
        for j, (_, bits) in enumerate(c["anchors"]):
            f += [(f"a{j}s{i}", b) for i, b in enumerate(bits)]
        return f

    def unpack(self, b):
        b = np.asarray(b)
        f, p = {}, 0
        for k, w in self.layout():
            f[k] = int("".join(map(str, b[p:p + w])), 2) if w else 0
            p += w
        return f

    # ------------------------------------------------------------ field quantizers
    def q_voicing(self, code, w):
        cost = (V_COST[np.asarray(code)[None, :], self.vpat] * w[None, :]).sum(1)
        return int(np.argmin(cost))

    def wc_value(self, i):
        b = self.cfg["bw"]
        return (i << (7 - b)) + ((1 << (7 - b)) >> 1)

    def pitch_line(self, wi, voiced):
        """Least-squares line through the voiced frames: (centre, slope) in index units."""
        N = self.N
        t = np.arange(N) - (N - 1) / 2
        v = np.where(voiced)[0]
        if len(v) == 0:
            return None
        if len(v) == 1:
            return float(wi[v[0]]), 0.0
        A = np.stack([np.ones(len(v)), t[v]], 1)
        c, s = np.linalg.lstsq(A, wi[v].astype(float), rcond=None)[0]
        return c, s

    def pitch_frames(self, wc, ws):
        N = self.N
        t = np.arange(N) - (N - 1) / 2
        return np.clip(np.round(self.wc_value(wc) + self.slopes[ws] * t), 0, 127).astype(int)

    def q_pitch(self, wi, voiced, prev_wc):
        """(centre index, slope index) minimising the voiced frames' squared index error."""
        line = self.pitch_line(wi, voiced)
        if line is None:
            return prev_wc, int(np.argmin(np.abs(self.slopes)))
        b = self.cfg["bw"]
        c0 = int(np.clip(np.round((line[0] - ((1 << (7 - b)) >> 1)) / (1 << (7 - b))), 0, (1 << b) - 1))
        best = None
        v = np.where(voiced)[0]
        for wc in range(max(0, c0 - 1), min((1 << b), c0 + 2)):
            for ws in range(len(self.slopes)):
                e = ((self.pitch_frames(wc, ws)[v] - wi[v]) ** 2).sum()
                if best is None or e < best[0]:
                    best = (e, wc, ws)
        return best[1], best[2]

    def energy_frames(self, em, er):
        lv = (1 << self.cfg["be"]) - 1
        m = em * 31 / lv
        return np.clip(np.round(m + self.eres[er]), 0, 31).astype(int)

    def q_energy(self, ei):
        lv = (1 << self.cfg["be"]) - 1
        m0 = int(np.round(ei.mean() * lv / 31))
        best = None
        for em in range(max(0, m0 - 1), min(lv, m0 + 1) + 1):
            for er in range(len(self.eres)):
                d = self.energy_frames(em, er) - ei
                e = (d ** 2).sum() + 4 * ((self.energy_frames(em, er) == 0) != (ei == 0)).sum()
                if best is None or e < best[0]:
                    best = (e, em, er)
        return best[1], best[2]

    # ------------------------------------------------------------ shape trellis
    def _cands(self, j, c):
        """M-best paths through anchor slot j's stages for DCT target c: (paths, recon)."""
        paths, rec = [[]], np.zeros((1, len(c)))
        for cb in self.scb[j]:
            res = c[None] - rec
            d = (res ** 2).sum(1)[:, None] - 2 * res @ cb.T + (cb ** 2).sum(1)[None]
            flat = np.argsort(d, axis=None)[:self.M_BEST]
            pi, ci = flat // cb.shape[0], flat % cb.shape[0]
            paths = [paths[i] + [int(k)] for i, k in zip(pi, ci)]
            rec = rec[pi] + cb[ci]
        return paths, rec

    def anchor_frames(self, ns):
        """Frame index and slot of every anchor, in time order."""
        out = []
        for s in range(ns):
            for j, (off, _) in enumerate(self.cfg["anchors"]):
                out.append((s * self.N + off, j))
        return out

    def q_shapes(self, cc, w):
        """Viterbi over the anchors' candidates. cc: DCT targets per frame (F, K-1)."""
        F = len(cc)
        anc = self.anchor_frames(F // self.N)
        C = []
        for f, j in anc:
            paths, rec = self._cands(j, cc[f])
            C.append((paths, rec))
        # cost of anchor k's candidates plus interpolated frames between anchor k-1 and k
        P0 = C[0][1]
        cost = w[anc[0][0]] * ((P0 - cc[anc[0][0]][None]) ** 2).sum(1)
        back = []
        for k in range(1, len(anc)):
            fa, fb = anc[k - 1][0], anc[k][0]
            Ra, Rb = C[k - 1][1], C[k][1]
            tot = cost[:, None] + w[fb] * ((Rb - cc[fb][None]) ** 2).sum(1)[None, :]
            for f in range(fa + 1, fb):
                t = (f - fa) / (fb - fa)
                r = (1 - t) * Ra[:, None, :] + t * Rb[None, :, :]
                tot = tot + w[f] * ((r - cc[f][None, None]) ** 2).sum(-1)
            back.append(np.argmin(tot, 0))
            cost = tot.min(0)
        # the frames after the last anchor hold it
        fl = anc[-1][0]
        RL = C[-1][1]
        for f in range(fl + 1, F):
            cost = cost + w[f] * ((RL - cc[f][None]) ** 2).sum(1)
        k = int(np.argmin(cost))
        choice = [k]
        for b in reversed(back):
            k = int(b[k])
            choice.append(k)
        choice.reverse()
        return [C[i][0][choice[i]] for i in range(len(anc))]

    # ------------------------------------------------------------ encoder
    def encode_params(self, p):
        N = self.N
        code, wi, ei, sh = (np.asarray(p[k]) for k in ("code", "wi", "ei", "shape"))
        nf = len(code)
        pad = (-nf) % N
        if pad:
            code, wi, ei = (np.concatenate([a, np.repeat(a[-1:], pad)]) for a in (code, wi, ei))
            sh = np.concatenate([sh, np.repeat(sh[-1:], pad, 0)])
        ns = len(code) // N
        w = frame_weight(ei)
        cc = (sh @ vl.DCT.T)[:, 1:]
        sel = self.q_shapes(cc, w)
        out, prev_wc, a = [], 64 >> (7 - self.cfg["bw"]), 0
        for s in range(ns):
            fr = slice(s * N, (s + 1) * N)
            v = dict(v=self.q_voicing(code[fr], w[fr]))
            dv = self.vpat[v["v"]]
            voiced = (dv != 0) & (code[fr] != 0)
            if not voiced.any():
                voiced = code[fr] != 0
            v["wc"], v["ws"] = self.q_pitch(wi[fr], voiced & (dv != 0) if (voiced & (dv != 0)).any() else voiced,
                                            prev_wc)
            prev_wc = v["wc"]
            v["em"], v["er"] = self.q_energy(ei[fr])
            for j in range(len(self.cfg["anchors"])):
                for i, idx in enumerate(sel[a]):
                    v[f"a{j}s{i}"] = idx
                a += 1
            out.append(self._pack([(v[k], wd) for k, wd in self.layout()]))
        return np.array(out, np.int8)

    def encode(self, x):
        return self.encode_params(self.params(x))

    # ------------------------------------------------------------ decoder
    def anchor_value(self, j, f):
        return sum(cb[f[f"a{j}s{i}"]] for i, cb in enumerate(self.scb[j]))

    def frame_params(self, bits, good=None):
        """Superframes -> per 20 ms frame dicts (code, wi, ei, shape). A lost superframe
        between two good ones (INTERP; the K6 plays from store, so the next one is known) is
        rebuilt from both: its shape follows the line between the neighbouring anchors, its
        pitch and energy run linearly from the previous frame to the next one, and its
        voicing copies the nearer neighbour. Other lost superframes repeat the last good one
        (concealment fades them). Sets self.rebuilt."""
        N = self.N
        ns = len(bits)
        good = np.ones(ns, bool) if good is None else np.asarray(good, bool)
        F, last = [], None
        for s in range(ns):
            if good[s]:
                last = self.unpack(bits[s])
            F.append(last)
        if F and F[0] is None:
            sil = self.unpack(np.zeros(self.BITS, int))
            F = [f if f is not None else sil for f in F]
        self.rebuilt = np.zeros(ns, bool)
        if self.INTERP:
            for s in range(1, ns - 1):
                self.rebuilt[s] = not good[s] and good[s - 1] and good[s + 1]
        # anchors on the frame grid (a rebuilt superframe has none of its own)
        afr, aval = [], []
        for s, f in enumerate(F):
            if self.rebuilt[s]:
                continue
            for j, (off, _) in enumerate(self.cfg["anchors"]):
                afr.append(s * N + off)
                aval.append(self.anchor_value(j, f))
        afr, aval = np.array(afr), np.array(aval)
        nf = ns * N
        c = np.zeros((nf, vl.K - 1))
        c[:afr[0]] = aval[0]
        for k in range(len(afr)):
            fa = afr[k]
            fb = afr[k + 1] if k + 1 < len(afr) else nf
            nb = aval[k + 1] if k + 1 < len(afr) else aval[k]
            for f in range(fa, fb):
                t = (f - fa) / (fb - fa)
                c[f] = (1 - t) * aval[k] + t * nb
        codes = np.zeros(nf, int)
        wis = np.zeros(nf, int)
        eis = np.zeros(nf, int)
        for s, f in enumerate(F):
            fr = slice(s * N, (s + 1) * N)
            codes[fr] = self.vpat[f["v"]]
            wis[fr] = self.pitch_frames(f["wc"], f["ws"])
            eis[fr] = self.energy_frames(f["em"], f["er"])
        for s in np.where(self.rebuilt)[0]:
            a, b = s * N - 1, (s + 1) * N                 # the frames either side
            fn = F[s + 1]
            ca, cb_ = codes[a], int(self.vpat[fn["v"]][0])
            wb = int(self.pitch_frames(fn["wc"], fn["ws"])[0])
            eb = int(self.energy_frames(fn["em"], fn["er"])[0])
            for k in range(N):
                t = (k + 1) / (N + 1)
                f = s * N + k
                codes[f] = ca if t < 0.5 else cb_
                if codes[f] == 3:                          # an edge needs its own neighbour
                    codes[f] = 2
                va, vb = ca != 0, cb_ != 0
                if va and vb:
                    wis[f] = int(round((1 - t) * wis[a] + t * wb))
                else:
                    wis[f] = wis[a] if va or not vb else wb
                eis[f] = int(round((1 - t) * eis[a] + t * eb))
        out = []
        for f in range(nf):
            z = np.zeros(vl.K)
            z[1:] = c[f]
            out.append(dict(code=int(codes[f]), wi=int(wis[f]), ei=int(eis[f]), shape=z @ vl.DCT))
        return out

    def decode(self, bits, good=None):
        fp = self.frame_params(bits, good)
        if good is not None:
            good = np.asarray(good, bool) | self.rebuilt
        return self._conceal(self._synth(fp), good)

    def _conceal(self, m, good):
        """v3.5-style concealment on 20 ms frames: hold 40 ms, fade 6 dB per 20 ms, silent
        after 80 ms; a lost superframe is N lost frames."""
        if good is None or np.all(good):
            return m
        run, r = [], 0
        for g in good:
            for _ in range(self.N):
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
