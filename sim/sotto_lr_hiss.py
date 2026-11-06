"""sotto 1200 with v3.8's 4-8 kHz hiss band: still 48 bits per 40 ms, 16 kHz in and out.

v3.8 (sim/hiss_band.py) found its bits in the 7 pitch bits of an all-unvoiced (UU) frame:
unvoiced halves are synthesised as noise harmonics 50 Hz apart, so the pitch of a UU frame
carries nothing. sotto 1200 has the same spare bits, in two sizes:

    A frame UU  (bits 2-8, pitch A)   6-bit hiss index into v3.8's 64 x 4 codebook + 1 side bit
    B frame UU  (bits 34-36, pitch B) 2-bit hiss code relative to the neighbouring A frames
                                      + 1 side bit

B's 2 bits (B_SIDE=True, the default) pick one of 4 level offsets added to the mean of the
quantised hiss of the UU anchors on either side of it (A of this superframe and of the next);
with no UU anchor the offset is added to the decoder's guess alone. Every UU frame then carries
a side bit, as in v3.8. B_SIDE=False spends all 3 bits on hiss (8 corrections) and only A
frames carry side bits; B_REL=False makes B's code an absolute correction (no anchors).
docs/sotto-1200-hiss.md compares them: 2 bits + side is the best of the four.

Everything else is v3.8's: the encoder's voicing rules (bursts and sibilants sent as UU,
creaky onsets as MM, whispered runs as UU, V/M flutter smoothing), the decoder's +3 dB on
rising unvoiced frames, the guess from the 0-4 kHz envelope, the vowel layer with the
per-speaker shape over the side channel, the blend. The 0-4 kHz part is sotto 1200's. A plain
sotto 1200 decoder (the K6) reads a hiss stream as sotto 1200 with UU frames' noise spaced by
the hiss index instead of the tracker's meaningless unvoiced pitch.
"""
import os
import numpy as np
from scipy.signal import resample_poly

import vocoder_lab as vl
import hiss_band as hb
from sotto_lr import Sotto1200

HERE = os.path.dirname(os.path.abspath(__file__))
B_MODEL = os.path.join(HERE, "data", "lr_hiss_b.npz")

# v3.8's candidate settings (docs/unvoiced-hiss.md, listening round 2 of 2026-11-02)
V38 = dict(vowel="speaker", whisper=True, vsmooth=True, sib_db=-10, sib_voiced=True,
           vowel_shape="fine", voiced_trim=-5, blend=True, release_ms=0, keep_mixed=True)


class Sotto1200Hiss(Sotto1200, hb.SottoV38):
    """encode() takes 16 kHz audio and returns 48-bit superframes; decode_audio16() returns
    16 kHz audio; decode() / decode_audio() are sotto 1200's 8 kHz decoder."""

    B_BITS = 3
    B_SIDE = True
    B_REL = True

    def __init__(self, b_model=B_MODEL, b_bits=None, b_side=None, b_rel=None, bcb=None, **kw):
        super().__init__(**{**V38, **kw})
        self.B_BITS = self.B_BITS if b_bits is None else b_bits
        self.B_SIDE = self.B_SIDE if b_side is None else b_side
        self.B_REL = self.B_REL if b_rel is None else b_rel
        nh = self.B_BITS - int(self.B_SIDE)
        if bcb is None and b_model and os.path.exists(b_model):
            z = np.load(b_model)
            key = f"{'rel' if self.B_REL else 'abs'}{nh}"
            bcb = z[key] if key in z else None
        self.bcb = np.zeros((2 ** nh, 4)) if bcb is None else np.asarray(bcb, float)
        assert len(self.bcb) == 2 ** nh

    # ------------------------------------------------------------ shared by both ends
    def _b_base(self, qa, qn):
        if not self.B_REL:
            return np.zeros(4)
        xs = [q for q in (qa, qn) if q is not None]
        return np.mean(xs, 0) if xs else np.zeros(4)

    @staticmethod
    def _rows(code, wi):
        """per-20 ms v3.7-style rows: voicing code in bits 0-1, a 7-bit pitch field in 2-8"""
        r = np.zeros((len(code), 48), np.int8)
        r[:, 0], r[:, 1] = np.asarray(code) >> 1, np.asarray(code) & 1
        for b in range(7):
            r[:, 2 + b] = (np.asarray(wi) >> (6 - b)) & 1
        return r

    def _side_rows(self, rows):
        """Rows as the side channel sees them: B frames hidden unless they carry a side bit."""
        if self.B_SIDE:
            return rows
        r = rows.copy()
        r[1::2, 0], r[1::2, 1] = 0, 1                  # not UU
        return r

    # ------------------------------------------------------------ encoder
    def _v38_rules(self, x8, x16, p):
        """v3.8's voicing rules (SottoV38.encode_low) on the per-20 ms fields p["code"] and
        p["wi"], in place; -> the 16 kHz STFT power."""
        code, wi = p["code"].copy(), p["wi"].copy()
        rows = self._rows(code, wi)
        # v3.8's voicing rules (SottoV38.encode_low) on the per-20 ms fields
        self._onsets(x8, rows)
        if self.whisper:
            self._whisper(x8, rows, lag=self.whisper_lag)
        if self.vsmooth:
            self._vsmooth(rows)
        P = np.abs(hb.stft(x16)) ** 2
        nfr = len(P)
        f = np.clip(np.arange(nfr) + hb.HB_LAG, 0, 2 * len(rows) - 1) // 2
        top, low = hb.bands_db(P, [(4000, 8000)])[:, 0], hb.bands_db(P, [(0, 3900)])[:, 0]
        for t in np.where((top[1:] - top[:-1] > hb.BURST_DB) & (top[1:] > low[1:] - 15))[0] + 1:
            rows[f[t], 0] = rows[f[t], 1] = 0
        if self.sib_db is not None:
            one = hb.bands_db(P, [(0, 1000)])[:, 0]
            c = rows[:, 0] * 2 + rows[:, 1]
            codes = (1, 2, 3) if self.sib_voiced else (2, 3)
            for t in np.where(top - one > self.sib_db)[0]:
                g = f[t]
                if g < len(rows) and c[g] in codes:
                    rows[g, 0] = rows[g, 1] = 0
        p["code"] = rows[:, 0].astype(int) * 2 + rows[:, 1]
        p["wi"] = np.array([hb.index_of(r, 7) for r in rows])
        return P

    def encode(self, x16):
        x16 = np.asarray(x16, float)
        x8 = resample_poly(x16, 1, 2)
        p = self.params(x8)
        P = self._v38_rules(x8, x16, p)
        nfr = len(P)
        sb = self.encode_params(p)
        self._sb_enc = sb
        m = Sotto1200.decode(self, sb)
        code = p["code"]
        rows = self._rows(code, np.zeros(len(code), int))
        hbands = hb.bands_db(P, hb.HB)
        E, v, f, uu = hb.frame_map(m, rows, nfr)
        res = hb.uu_residuals(hbands, hb.guess(self.W, E, v), f, uu)
        ns = len(sb)
        q, ia = {}, {}
        for s in range(ns):                            # A frames: v3.8's 6-bit index
            g = 2 * s
            if code[g] == 0:
                ia[s] = int(np.argmin(((self.cb - res[g]) ** 2).sum(1))) if g in res else 0
                q[g] = self.cb[ia[s]]
        ib, self._b_targets = {}, []
        for s in range(ns):                            # B frames: relative to the quantised anchors
            g = 2 * s + 1
            if code[g] == 0:
                base = self._b_base(q.get(g - 1), q.get(g + 1))
                if g in res:
                    self._b_targets.append(res[g] - base)  # for tools/train_lr_hiss.py
                ib[s] = int(np.argmin(((base + self.bcb - res[g]) ** 2).sum(1))) if g in res else 0
        # side channel (v3.8's speaker shape): one bit per UU A frame (and B frame if B_SIDE)
        srows = self._side_rows(rows)
        self._side_pc(srows, x16)
        lay = self.layout()
        out = []
        for s in range(ns):
            fl = self._fields(sb[s])
            if s in ia:
                fl["wa"] = (ia[s] << 1) | int(srows[2 * s, 8])
            if s in ib:
                fl["dw"] = (ib[s] << 1) | int(srows[2 * s + 1, 8]) if self.B_SIDE else ib[s]
            out.append(self._pack([(fl[k], w) for k, w in lay]))
        return np.array(out, np.int8)

    def _fields(self, bits):
        f, p = {}, 0
        for k, w in self.layout():
            f[k] = int("".join(map(str, bits[p:p + w])), 2)
            p += w
        return f

    def _low16(self, bits, n16):
        """The decoder's own 0-4 kHz output at 16 kHz (side channel, encoder side)."""
        y8 = vl.synthesise(Sotto1200.decode(self, self._sb_enc))[vl.ar.CODEC_DELAY:]
        n8 = n16 // 2
        return resample_poly(np.pad(y8, (0, max(0, n8 - len(y8))))[:n8], 2, 1)

    # ------------------------------------------------------------ decoder
    def decode_audio16(self, sb, good=None, n16=None, seed=0):
        sb = np.asarray(sb)
        ns = len(sb)
        good = np.ones(ns, bool) if good is None else np.asarray(good, bool)
        fp = self.frame_params(sb, good)               # sets self.rebuilt
        code = np.array([q["code"] for q in fp])
        F, last = [], None
        for s in range(ns):
            if good[s]:
                last = self._fields(sb[s])
            F.append(last)
        res, side = np.zeros((2 * ns, 4)), np.zeros(2 * ns, int)
        for s in range(ns):                            # A first: B needs both anchors
            if F[s] is not None and code[2 * s] == 0 and not self.rebuilt[s]:
                res[2 * s] = self.cb[F[s]["wa"] >> 1]
                side[2 * s] = F[s]["wa"] & 1
        isq = lambda g: res[g] if 0 <= g < 2 * ns and code[g] == 0 else None
        for s in np.where(self.rebuilt)[0]:            # rebuilt A: halfway between the neighbours'
            if code[2 * s] == 0:
                res[2 * s] = self._b_base(isq(2 * s - 2), isq(2 * s + 2)) if self.B_REL else 0
        for s in range(ns):
            g = 2 * s + 1
            if F[s] is None or code[g] != 0:
                continue
            base = self._b_base(isq(g - 1), isq(g + 1))
            if self.rebuilt[s]:
                res[g] = base
            else:
                dw = F[s]["dw"]
                res[g] = base + self.bcb[dw >> 1 if self.B_SIDE else dw]
                side[g] = dw & 1 if self.B_SIDE else 0
        self._res = res
        rows = self._rows(code, side)                  # side bit lands in bit 8
        rgood = np.repeat(good | self.rebuilt, 2)
        self._side_good = np.repeat(good, 2)          # a rebuilt frame carries no side bit
        self._dec_args = (sb, good)
        return hb.SottoV38.decode_audio16(self, rows, rgood, n16=n16, seed=seed)

    @staticmethod
    def _conceal_rows(rows, good):
        """The rows are already concealed (frame_params repeats the last good superframe), so
        a lost frame keeps its own repeated code and hiss; run counts lost 20 ms frames."""
        run, r = np.zeros(len(rows), int), 0
        for g in range(len(rows)):
            r = 0 if good[g] else r + 1
            run[g] = r
        return rows.copy(), run

    def _hiss_model(self, rows, good):
        return Sotto1200.decode(self, *self._dec_args)

    def _uu_res(self, rows, g):
        return self._res[g]

    def _read_pc(self, bits, good):
        return hb.SottoV38._read_pc(self, self._side_rows(bits), self._side_good)
