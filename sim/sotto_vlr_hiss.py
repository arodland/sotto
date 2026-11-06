"""sotto 600 / 400 with v3.8's 4-8 kHz hiss band: still 48 bits per 80 / 120 ms, 16 kHz in and out.

As in sotto 1200 hiss (sim/sotto_lr_hiss.py) the bits come from pitch fields that carry nothing
(docs/sotto-vlr.md, "Hiss band"):

    no pitched frame   (pattern all U)   pitch centre (7 bits) = v3.8's 6-bit hiss index + 1 side
                                         bit; pitch slope (3 bits) = a hiss level slope across
                                         the superframe (8 trained dB-per-frame steps)
    one pitched frame                    the slope means nothing (one point has no slope):
                                         its 3 bits = a 2-bit level offset over the neighbours'
                                         quantised hiss + 1 side bit; the pitch is the centre
    two or more                          no bits: the unvoiced frames take the neighbours' hiss

"Pitched" = the decoded voicing pattern's code is not U (V, M or the M-then-U edge). The
neighbours' hiss is the mean of the nearest explicitly coded frame before and after (within
2 superframes); with neither the residual is 0 (the decoder's guess alone). Each superframe
that carries hiss carries one side bit for v3.8's speaker shape. Everything else is v3.8's
candidate (sotto_lr_hiss.V38): encoder voicing rules applied to the per-20 ms codes before the
pattern search, guess, vowel layer, blend. SLOPE_HISS=False keeps the slope field as pitch in
one-pitched-frame superframes (then a plain decoder reads the stream with right pitch).
"""
import os
import numpy as np
from scipy.signal import resample_poly

import vocoder_lab as vl
import hiss_band as hb
from sotto_vlr import SottoVLR
from sotto_lr_hiss import Sotto1200Hiss, V38, B_MODEL

HERE = os.path.dirname(os.path.abspath(__file__))
VLR_HISS = os.path.join(HERE, "data", "vlr_hiss.npz")
# default level slopes (dB per 20 ms frame), replaced by trained ones when vlr_hiss.npz exists
SLOPES = np.array([-8.0, -4.0, -2.0, -0.7, 0.7, 2.0, 4.0, 8.0])


class SottoVLRHiss(SottoVLR, hb.SottoV38):
    """encode() takes 16 kHz audio and returns 48-bit superframes; decode_audio16() returns
    16 kHz audio; decode() / decode_audio() are sotto 600 / 400's 8 kHz decoder."""

    SLOPE_HISS = True
    REACH = 2                                   # superframes either side for the neighbours' hiss

    def __init__(self, mode="600", slope_hiss=None, model=VLR_HISS, side=True, **kw):
        super().__init__(mode=mode, **{**V38, **kw})
        assert self.cfg["bw"] == 7 and self.cfg["bs"] == 3
        self.SLOPE_HISS = self.SLOPE_HISS if slope_hiss is None else slope_hiss
        self.side = side
        z = np.load(model) if model and os.path.exists(model) else {}
        self.hsl = np.asarray(z[f"m{mode}_slope"], float) if f"m{mode}_slope" in z else SLOPES
        self.hoff = np.asarray(z[f"m{mode}_off"], float) if f"m{mode}_off" in z else \
            np.load(B_MODEL)["rel2"]
        # no coded neighbour: h0 (the mean residual there); kind-2 frames: + k2 over the base
        self.h0 = np.asarray(z[f"m{mode}_h0"], float) if f"m{mode}_h0" in z else np.zeros(4)
        self.k2 = np.asarray(z[f"m{mode}_k2"], float) if f"m{mode}_k2" in z else np.zeros(4)
        self.t = np.arange(self.N) - (self.N - 1) / 2

    # ------------------------------------------------------------ shared by both ends
    _rows = staticmethod(Sotto1200Hiss._rows)

    def npitched(self, f):
        return int((self.vpat[f["v"]] != 0).sum())

    def kind(self, f):
        """0: hiss in pitch centre + slope; 1: offset in the slope; 2: none"""
        n = self.npitched(f)
        return 0 if n == 0 else 1 if n == 1 and self.SLOPE_HISS else 2

    def sf_pitch(self, f):
        if self.SLOPE_HISS and self.npitched(f) == 1:
            return np.full(self.N, int(np.clip(self.wc_value(f["wc"]), 0, 127)))
        return SottoVLR.sf_pitch(self, f)

    def _base(self, q, g, nf, raw=False):
        """mean of the nearest explicitly coded frame before and after g; with none, h0
        (raw: (mean or zeros, found))"""
        r = self.REACH * self.N
        xs = []
        for rng in (range(g - 1, max(-1, g - r - 1), -1), range(g + 1, min(nf, g + r + 1))):
            h = next((h for h in rng if h in q), None)
            if h is not None:
                xs.append(q[h])
        if raw:
            return (np.mean(xs, 0), True) if xs else (np.zeros(4), False)
        return np.mean(xs, 0) if xs else self.h0.copy()

    def _res2(self, q, g, nf):
        """the hiss of a U frame with no bits of its own"""
        b, ok = self._base(q, g, nf, raw=True)
        return b + self.k2 if ok else self.h0.copy()

    def _carriers(self, F, code, skip):
        """the side-bit frame of each superframe that carries hiss: its first U frame"""
        out = {}
        for s, f in enumerate(F):
            if f is None or skip[s] or self.kind(f) == 2:
                continue
            fr = np.where(code[s * self.N:(s + 1) * self.N] == 0)[0]
            if len(fr):
                out[s] = s * self.N + int(fr[0])
        return out

    def _side_view(self, rows):
        """rows as the side channel sees them: only carrier frames are UU"""
        r = rows.copy()
        keep = np.zeros(len(r), bool)
        keep[list(self._carr.values())] = True
        r[~keep & (r[:, 0] == 0) & (r[:, 1] == 0), 1] = 1
        return r

    # ------------------------------------------------------------ encoder
    def encode(self, x16):
        x16 = np.asarray(x16, float)
        x8 = resample_poly(x16, 1, 2)
        p = self.params(x8)
        P = Sotto1200Hiss._v38_rules(self, x8, x16, p)
        nfr = len(P)
        sb = SottoVLR.encode_params(self, p)
        N, ns = self.N, len(sb)
        F = [self.unpack(b) for b in sb]
        code_in, wi_in = np.asarray(p["code"]), np.asarray(p["wi"])
        for s, f in enumerate(F):                  # one pitched frame: pitch = centre, no slope
            if self.kind(f) != 1:
                continue
            k = int(np.where(self.vpat[f["v"]] != 0)[0][0])
            fr = np.arange(s * N, min(len(code_in), (s + 1) * N))
            g = s * N + k
            if g < len(code_in) and code_in[g] != 0:
                target = wi_in[g]
            elif (code_in[fr] != 0).any():
                target = np.median(wi_in[fr][code_in[fr] != 0])
            else:
                target = SottoVLR.sf_pitch(self, f)[k]
            f["wc"] = int(np.argmin(np.abs(self.wc_value(np.arange(128)) - target)))
            f["ws"] = 0
        lay = self.layout()
        sb = np.array([self._pack([(f[k], w) for k, w in lay]) for f in F], np.int8)
        self._sb_enc = sb
        fp = self.frame_params(sb)
        code = np.array([q["code"] for q in fp])
        nf = len(code)
        m = SottoVLR.decode(self, sb)
        rows = Sotto1200Hiss._rows(code, np.zeros(nf, int))
        E, v, fmap, uu = hb.frame_map(m, rows, nfr)
        res = hb.uu_residuals(hb.bands_db(P, hb.HB), hb.guess(self.W, E, v), fmap, uu)
        q, self._targets = {}, dict(slope=[], off=[], off_raw=[], k2=[])
        for s, f in enumerate(F):                  # no pitched frame: index + slope
            if self.kind(f) != 0:
                continue
            ks = [k for k in range(N) if s * N + k in res]
            if not ks:
                f["hi"], f["hs"] = 0, int(np.argmin(np.abs(self.hsl)))
            else:
                R = np.array([res[s * N + k] for k in ks])
                tt = self.t[ks]
                # joint search: index for the mean shape, slope for the level's course
                e = ((self.cb[:, None, None, :] + self.hsl[None, :, None, None] * tt[None, None, :, None]
                      - R[None, None]) ** 2).sum((2, 3))
                i, j = np.unravel_index(int(np.argmin(e)), e.shape)
                f["hi"], f["hs"] = int(i), int(j)
                if len(ks) > 1:                    # for training: the level's own slope
                    lv = (R - self.cb[i]).mean(1)
                    self._targets["slope"].append(float(np.polyfit(tt, lv, 1)[0]))
            for k in range(N):
                q[s * N + k] = self.cb[f["hi"]] + self.hsl[f["hs"]] * self.t[k]
        for s, f in enumerate(F):                  # one pitched frame: offset over the neighbours
            if self.kind(f) != 1:
                continue
            gs = [g for g in range(s * N, (s + 1) * N) if code[g] == 0 and g in res]
            if not gs:
                f["ho"] = 0
                continue
            base = np.array([self._base(q, g, nf) for g in gs])
            R = np.array([res[g] for g in gs])
            f["ho"] = int(np.argmin(((base[None] + self.hoff[:, None, :] - R[None]) ** 2).sum((1, 2))))
            self._targets["off"].append((R - base).mean(0))
            self._targets["off_raw"].append([(res[g] - b, ok) for g in gs for b, ok in [self._base(q, g, nf, raw=True)]])
        for s, f in enumerate(F):                  # for training: frames with no bits
            if self.kind(f) == 2:
                for g in range(s * N, (s + 1) * N):
                    if code[g] == 0 and g in res:
                        b, ok = self._base(q, g, nf, raw=True)
                        self._targets["k2"].append((res[g] - b, ok))
        # side channel: one bit per superframe that carries hiss
        self._carr = self._carriers(F, code, np.zeros(ns, bool))
        srows = self._side_view(rows)
        if self.vowel == "speaker" and self.side:
            self._side_pc(srows, x16)
        out = []
        for s, f in enumerate(F):
            k = self.kind(f)
            sbit = int(srows[self._carr[s], 8]) if s in self._carr else 0
            if k == 0:
                f["wc"], f["ws"] = (f["hi"] << 1) | sbit, f["hs"]
            elif k == 1:
                f["ws"] = (f["ho"] << 1) | sbit
            out.append(self._pack([(f[kk], w) for kk, w in lay]))
        return np.array(out, np.int8)

    def _low16(self, bits, n16):
        """the decoder's own 0-4 kHz output at 16 kHz (side channel, encoder side)"""
        y8 = vl.synthesise(SottoVLR.decode(self, self._sb_enc))[vl.ar.CODEC_DELAY:]
        n8 = n16 // 2
        return resample_poly(np.pad(y8, (0, max(0, n8 - len(y8))))[:n8], 2, 1)

    # ------------------------------------------------------------ decoder
    def decode_audio16(self, sb, good=None, n16=None, seed=0):
        sb = np.asarray(sb)
        N, ns = self.N, len(sb)
        good = np.ones(ns, bool) if good is None else np.asarray(good, bool)
        fp = self.frame_params(sb, good)           # sets self.rebuilt
        code = np.array([q["code"] for q in fp])
        nf = len(code)
        F = [self.unpack(sb[s]) if good[s] else None for s in range(ns)]
        q, side = {}, np.zeros(nf, int)
        for s, f in enumerate(F):
            if f is not None and self.kind(f) == 0:
                for k in range(N):
                    q[s * N + k] = self.cb[f["wc"] >> 1] + self.hsl[f["ws"]] * self.t[k]
        self._carr = self._carriers(F, code, self.rebuilt)
        res = np.zeros((nf, 4))
        for s in range(ns):
            f = F[s]
            k = self.kind(f) if f is not None else 2
            for g in range(s * N, (s + 1) * N):
                if code[g] != 0:
                    continue
                if k == 0:
                    res[g] = q[g]
                elif k == 1:
                    res[g] = self._base(q, g, nf) + self.hoff[f["ws"] >> 1]
                else:
                    res[g] = self._res2(q, g, nf)
            if s in self._carr:
                side[self._carr[s]] = (f["wc"] if k == 0 else f["ws"]) & 1
        self._res = res
        rows = Sotto1200Hiss._rows(code, side)    # side bit lands in bit 8
        rgood = np.repeat(good | self.rebuilt, N)
        self._dec_args = (sb, good)
        return hb.SottoV38.decode_audio16(self, rows, rgood, n16=n16, seed=seed)

    def _hiss_model(self, rows, good):
        return SottoVLR.decode(self, *self._dec_args)

    def _uu_res(self, rows, g):
        return self._res[g]

    def _read_pc(self, bits, good):
        if not self.side:
            return np.zeros((len(bits), 3))
        return hb.SottoV38._read_pc(self, self._side_view(bits), good)
