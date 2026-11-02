"""s model experiments on top of v3.8 + hiss ceiling (ceiling_air.SottoV38S):
  cb16:     UU hiss index read as a 16-bin (250 Hz) residual from the interpolated guess
            instead of 4 band levels (same 6 bits; table 64 x 16), encoder searches 16 bins;
  rayleigh: the hiss's per-bin magnitudes get Rayleigh-distributed fluctuation each frame
            (real noise), instead of exactly the envelope with random phase only.
The decoder is v3.8's decode_audio16 with three lines changed (patched from its source)."""
import os, sys, inspect, textwrap, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling as cl, ceiling_air as ca
hb = cl.hb
I16 = np.stack([np.interp(cl.FC, hb.HBC, e) for e in np.eye(4)], 0)

_src = textwrap.dedent(inspect.getsource(hb.SottoV38.decode_audio16))
for a, b in [("p[t] += self.cb[index_of(b2[src], self.nb)]",
              "p[t] += self.cb[index_of(b2[src], self.nb)]; src16[t] = src"),
             ("mask = np.zeros(nfr)", "mask = np.zeros(nfr); src16 = np.full(nfr, -1)"),
             ("lvl = np.stack([np.interp(FR, HBC, h) for h in p]) if nfr else np.zeros((0, len(FR)))",
              "lvl = self._levels(p, src16, nfr, b2)"),
             ("X = mag * ph", "X = mag * ph * self._texture(mag.shape, seed)")]:
    assert _src.count(a) == 1, a
    _src = _src.replace(a, b)
_ns = dict(vars(hb)); exec(_src, _ns)


class SottoS(ca.SottoV38S):
    def __init__(self, cb16=None, rayleigh=False, **kw):
        super().__init__(**kw)
        self.cb16, self.rayleigh = (None if cb16 is None else np.asarray(cb16)), rayleigh

    decode_audio16_v38 = _ns["decode_audio16"]

    def _levels(self, p, src16, nfr, b2):
        if not nfr: return np.zeros((0, len(hb.FR)))
        lvl = np.stack([np.interp(hb.FR, hb.HBC, h) for h in p])
        if self.cb16 is not None:
            for t in np.where(src16 >= 0)[0]:
                i = hb.index_of(b2[src16[t]], self.nb)
                # p[t] already holds guess + cb4 + the frame's adjustments; swap cb4 for cb16
                lvl[t] = np.interp(hb.FR, cl.FC, (p[t] - self.cb[i]) @ I16 + self.cb16[i])
        return lvl

    def _texture(self, shape, seed):
        if not self.rayleigh: return 1.0
        rng = np.random.default_rng(seed + 7)
        return np.abs(rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) / np.sqrt(2)

    def decode_audio16(self, bits, good=None, n16=None, seed=0):
        y = self.decode_audio16_v38(bits, good, n16, seed)
        ceil = self._frame_ceiling(np.asarray(bits), None if good is None else np.asarray(good, bool))
        if all(c is None for c in ceil): return y
        X = hb.stft(np.pad(y, (hb.H, hb.N)))
        for t in range(len(X)):
            g = min(max(t - 1 + hb.HB_LAG, 0) // 2, len(ceil) - 1)
            if ceil[g] is not None: X[t] *= 10 ** (cl.gain_db(hb.FR, *ceil[g]) / 20)
        return hb.istft(X, len(y) + hb.H + hb.N)[hb.H:hb.H + len(y)]

    # encoder: with cb16, every UU index is searched in 16 bins (with the ceiling in force)
    def encode(self, x16):
        bits = super().encode(x16)
        if self.cb16 is None: return bits
        ceil = self._frame_ceiling(bits)
        _, hbands, m = self.encode_low(x16)
        P = np.abs(hb.stft(x16)) ** 2
        E, v, f, uu = hb.frame_map(m, bits, len(P))
        p = hb.guess(self.W, E, v)
        T = cl.fine(P)
        code = bits[:, 0] * 2 + bits[:, 1]; nb = self.nb
        for g in np.where(code == 0)[0]:
            ts = np.where((f == g) & uu)[0]
            if not len(ts): continue
            G = np.zeros(16) if ceil[g] is None else cl.gain_db(cl.FC, *ceil[g])
            err = np.zeros(len(self.cb16))
            for t in ts:
                fl = T[t].max() - 40
                dec = (p[t] @ I16)[None] + self.cb16 + G[None]
                err += ((np.maximum(dec, fl) - np.maximum(T[t], fl)) ** 2).sum(1)
            i = int(np.argmin(err))
            bits[g, 2:2 + nb] = [(i >> (nb - 1 - b)) & 1 for b in range(nb)]
        return bits


# ---------------------------------------------------------------------------- 14-bit s shape
# UU frames give up the 4th 0-4 kHz shape stage (8 bits, bits 40..47) and use them for a
# second hiss stage: a 256 x 16 table of 250 Hz-bin refinements added to v3.8's 4-band level
# line (k-means on LibriSpeech dev-clean training speakers' UU frames, s frames weighted x3).
HERE = os.path.dirname(os.path.abspath(__file__))
S4 = slice(40, 48)


class SottoS14(SottoS):
    def __init__(self, cb2=None, **kw):
        super().__init__(**kw)
        self.cb2 = np.load(os.path.join(HERE, "cb2.npy")) if cb2 is None else cb2
        self._last_code = None

    # decoder, 0-4 kHz: UU frames decode the shape from stages 1-3 only
    def _read_voicing(self, code, prev):
        self._last_code = code
        return super()._read_voicing(code, prev)

    def _shape_val(self, idx):
        if self._last_code == 0:
            vq = self.sq.vq
            c = np.zeros(hb.vl.K)
            c[1:] = sum(vq.cb[s][idx[s]] for s in range(3))
            return c @ hb.vl.DCT
        return super()._shape_val(idx)

    # decoder, 4-8 kHz: the refinement joins the level line
    def _levels(self, p, src16, nfr, b2):
        lvl = np.stack([np.interp(hb.FR, hb.HBC, h) for h in p]) if nfr else np.zeros((0, len(hb.FR)))
        for t in np.where(src16 >= 0)[0]:
            r = int("".join(map(str, b2[src16[t], S4])), 2)
            lvl[t] = np.interp(hb.FR, cl.FC, p[t] @ I16 + self.cb2[r])
        return lvl

    # encoder: base (v3.8 + ceiling over the air), then re-search both hiss stages from the
    # decoder's view (0-4 kHz model with UU frames on 3 shape stages)
    def encode(self, x16):
        bits = ca.SottoV38S.encode(self, x16)
        if getattr(self, 'research', False): self._research_uu_shape(bits, x16)   # tried: NB-PESQ 2.829 -> 2.807, off
        ceil = self._frame_ceiling(bits)
        _, hbands, _ = self.encode_low(x16)
        m = self.decode(bits)                       # decoder's model (UU frames: 3 stages)
        P = np.abs(hb.stft(x16)) ** 2
        E, v, f, uu = hb.frame_map(m, bits, len(P))
        p = hb.guess(self.W, E, v)
        T = cl.fine(P)
        Dcb = self.cb @ I16
        code = bits[:, 0] * 2 + bits[:, 1]; nb = self.nb
        for g in np.where(code == 0)[0]:
            ts = np.where((f == g) & uu)[0]
            if not len(ts): continue
            G = np.zeros(16) if ceil[g] is None else cl.gain_db(cl.FC, *ceil[g])
            fl = [T[t].max() - 40 for t in ts]
            # stage 1: 4-band index; ceiling-aware 16-bin search (as v3.8 + ceiling)
            err = sum(((np.maximum((p[t] @ I16)[None] + Dcb + G, fl[k]) - np.maximum(T[t], fl[k])) ** 2).sum(1) for k, t in enumerate(ts))
            i = int(np.argmin(err))
            # stage 2: 16-bin refinement
            err2 = sum(((np.maximum(((p[t] + self.cb[i]) @ I16)[None] + self.cb2 + G, fl[k]) - np.maximum(T[t], fl[k])) ** 2).sum(1) for k, t in enumerate(ts))
            j = int(np.argmin(err2))
            bits[g, 2:2 + nb] = [(i >> (nb - 1 - b)) & 1 for b in range(nb)]
            bits[g, S4] = [(j >> (7 - b)) & 1 for b in range(8)]
        return bits

    def _research_uu_shape(self, bits, x16):
        """UU frames: search the 0-4 kHz shape again with stages 1-3 only (M-best)."""
        import copy
        from scipy.signal import resample_poly
        vq3 = copy.copy(self.sq.vq); vq3.cb = self.sq.vq.cb[:3]; vq3.bits = self.sq.vq.bits[:3]
        m = hb.vl.analyse(resample_poly(x16, 1, 2), self.pitch)
        if len(m) % 2: m = np.concatenate([m, m[-1:]])
        rk = hb.vl.to_rate_k(m)
        code = bits[:, 0] * 2 + bits[:, 1]
        for g in np.where(code == 0)[0]:
            if 2 * g >= len(rk): continue
            idx = vq3.indices(rk[2 * g] - rk[2 * g].mean())[0]
            p = 14
            for i, nb in zip(idx, vq3.bits):
                bits[g, p:p + nb] = [(int(i) >> (nb - 1 - k)) & 1 for k in range(nb)]
                p += nb
