"""sotto v3.9 candidate: two layers on top of v3.8's 4-8 kHz hiss (docs/hiss-ceiling.md).

1. Hiss ceiling (per speaker / microphone, over the side channel). When the input stops at
   some frequency fc between 4.25 and 7.75 kHz (a dull microphone, an MP3 source), the whole
   4-8 kHz layer gets -min(S (f - fc), 60) dB. The encoder measures (fc, S) causally on its
   bright frames; the pair rides v3.8's side channel as a 16-bit message whose CRC-4 is XORed
   with CEIL_MASK, so it needs no type bit and a v3.8 decoder ignores it. With a ceiling in
   force, the encoder picks each UU frame's hiss index against the 250 Hz-bin spectrum the
   decoder will make. A voice without a ceiling gets exactly v3.8's bits.
2. Non-s cap (decoder only). A UU, mixed or edge frame whose hiss sits more than CAP_MARGIN dB
   under its 2-4 kHz level (sh, f, th, z, v: anything that is not an s) loses CAP_S dB/kHz
   above CAP_FC (at most CAP_DEPTH dB), smoothed over 3 frames. Off while a ceiling is in force.
Listening rounds 14-17: ceiling 7-0; cap 4-0 (1 tie); soft cap over hard; the cap also on z;
ceiling alone over ceiling + cap on an MP3-cut voice.
"""
import inspect
import textwrap
import numpy as np

import hiss_band as hb

# ---------------------------------------------------------------- ceiling
CEIL_FCS = 4250.0 + 250.0 * np.arange(15)     # cut-off codes 0..14; 15 = no ceiling
CEIL_SLOPES = np.array([48.0, 96.0, 48.0, 96.0])   # steepness codes (2, 3 spare = 0, 1)
CEIL_DEPTH = 60.0
CEIL_MASK = [1, 0, 1, 0]
CEIL_REPEAT = 5               # windows between repeats of an unchanged ceiling
CEIL_MIN = 10                 # bright frames before the first decision
CEIL_GAIN = 3.0               # dB the fit must improve to declare a ceiling
FB = np.arange(4000, 8000, 250)
FC = FB + 125.0
I16 = np.stack([np.interp(FC, hb.HBC, e) for e in np.eye(4)], 0)    # 4 band levels -> 16 bins
# ---------------------------------------------------------------- non-s cap
CAP_FC, CAP_S, CAP_DEPTH = 5000.0, 24.0, 40.0
# The test runs on the synthesis-side layer, which reads ~2 dB hotter above 4 kHz than the
# decoded output; a 1 dB margin here caps the same frames as rounds 16-17's 3 dB on the output.
CAP_MARGIN = 1.0


def ceil_gain_db(f, c):
    if c is None:
        return np.zeros_like(np.asarray(f, float))
    return -np.clip((np.asarray(f, float) - c[0]) * c[1] / 1000.0, 0.0, CEIL_DEPTH)


def fine(P):
    """power spectra -> 16 bins of 250 Hz, 4-8 kHz, dB"""
    return np.stack([10 * np.log10(P[..., (hb.FR >= a) & (hb.FR < a + 250)].mean(-1) + 1e-12) for a in FB], -1)


def fit_ceiling(Fsum, n):
    """Mean spectrum (16 bins of power, each frame re its 4-6 kHz level) -> (fc, S) or None.
    Fit a line, and a line plus each ceiling, both floored 50 dB under the peak; keep the best
    ceiling if it cuts the rms misfit by CEIL_GAIN dB."""
    if n < CEIL_MIN:
        return None
    m = 10 * np.log10(Fsum / n + 1e-20)
    fl = m.max() - 50.0
    mc = np.maximum(m, fl)
    f = FC / 1000.0

    def misfit(G):
        w = G > -1
        if w.sum() < 3:
            return 1e9
        b, a = np.polyfit(f[w], m[w], 1)
        return np.sqrt(((np.maximum(a + b * f + G, fl) - mc) ** 2).mean())

    base = misfit(np.zeros(len(f)))
    best = min((misfit(ceil_gain_db(FC, (fc, s))), fc, s) for fc in CEIL_FCS for s in CEIL_SLOPES[:2])
    return (float(best[1]), float(best[2])) if base - best[0] >= CEIL_GAIN else None


def pack_ceiling(c):
    fi = 15 if c is None else int(np.argmin(np.abs(CEIL_FCS - c[0])))
    si = 0 if c is None else int(np.argmin(np.abs(CEIL_SLOPES[:2] - c[1])))
    b = [(fi >> (3 - i)) & 1 for i in range(4)] + [(si >> (1 - i)) & 1 for i in range(2)] + [0] * 6
    return b + [x ^ k for x, k in zip(hb.crc4(b), CEIL_MASK)]


def unpack_ceiling(b):
    """-> (fc, S), None (no ceiling), or False (not a ceiling message)"""
    if len(b) < 16 or [x ^ k for x, k in zip(hb.crc4(b[:12]), CEIL_MASK)] != list(b[12:16]):
        return False
    fi = int("".join(map(str, b[0:4])), 2)
    si = int("".join(map(str, b[4:6])), 2)
    return None if fi == 15 else (float(CEIL_FCS[fi]), float(CEIL_SLOPES[si]))


# v3.8's decoder with one line changed: the finished 4-8 kHz layer X goes through _post_hiss
_src = textwrap.dedent(inspect.getsource(hb.SottoV38.decode_audio16))
for _a, _b in [("    return y16 + istft(X, len(y16))\n",
                "    X = self._post_hiss(X, y16, b2, bits, good, f, nfr)\n    return y16 + istft(X, len(y16))\n")]:
    assert _src.count(_a) == 1, _a
    _src = _src.replace(_a, _b)
_ns = dict(vars(hb))
exec(_src, _ns)


class SottoV39(hb.SottoV38):
    """v3.8 + hiss ceiling + non-s cap. Same 48-bit frame and constructor arguments as
    SottoV38 (use vowel="speaker": the ceiling rides its side channel). cap=False and
    ceiling=False switch the layers off for comparisons."""

    def __init__(self, cap=True, ceiling=True, **kw):
        super().__init__(**kw)
        self.cap, self.ceiling = cap, ceiling

    # ------------------------------------------------------------ encoder
    def encode(self, x16):
        self._n16 = len(x16)
        bits = super().encode(x16)                     # v3.8, with the ceiling messages (_side_pc)
        ceil = self.frame_ceiling(bits)
        if any(c is not None for c in ceil):
            self._index_search(bits, x16, ceil)
        return bits

    def _side_pc(self, bits, x16):
        super()._side_pc(bits, x16)                    # vowel-shape messages in every window
        if not self.ceiling:
            return
        code = bits[:, 0] * 2 + bits[:, 1]
        for w, (c, send) in enumerate(self.ceiling_schedule(x16, len(bits))):
            if send:                                   # this window carries the ceiling instead
                uuf = [g for g in range(w * hb.WIN_FRAMES, min(len(bits), (w + 1) * hb.WIN_FRAMES)) if code[g] == 0]
                for g, b in zip(uuf, pack_ceiling(c)):
                    bits[g, 8] = b

    @staticmethod
    def ceiling_schedule(x16, nbits):
        """Per window: the ceiling decided from the windows before it, and whether it is sent.
        Bright frames: louder than the running median frame energy, 4-8 kHz within 20 dB of
        0-1 kHz. Sent when the decision changes, then every CEIL_REPEAT windows."""
        P = np.abs(hb.stft(x16)) ** 2
        tot = 10 * np.log10(P.sum(1) + 1e-12)
        lo = hb.bands_db(P, [(0, 1000)])[:, 0]
        hi = hb.bands_db(P, [(4000, 8000)])[:, 0]
        bright = hb.running_pct(tot, 50, warm=50) & (hi > lo - 20)
        F = fine(P)
        F = 10 ** ((F - 10 * np.log10(np.mean(10 ** (F[:, :8] / 10), 1))[:, None]) / 10)
        fr = np.clip(np.arange(len(P)) + hb.HB_LAG, 0, 2 * nbits - 1) // 2
        acc, n, cur, last, out = np.zeros(len(FB)), 0, None, -99, []
        for w in range((nbits + hb.WIN_FRAMES - 1) // hb.WIN_FRAMES):
            dec = fit_ceiling(acc, n)
            send = (dec != cur) or (dec is not None and w - last >= CEIL_REPEAT)
            if send:
                last = w
            cur = dec
            out.append((cur, send))
            ts = np.where((fr >= w * hb.WIN_FRAMES) & (fr < (w + 1) * hb.WIN_FRAMES) & bright)[0]
            acc += F[ts].sum(0)
            n += len(ts)
        return out

    def _index_search(self, bits, x16, ceil):
        """UU frames under a ceiling: the 6-bit index that best matches the 250 Hz bins the
        decoder will make (4-band line + ceiling), each frame floored 40 dB under its peak."""
        _, _, m = self.encode_low(x16)
        P = np.abs(hb.stft(x16)) ** 2
        E, v, f, uu = hb.frame_map(m, bits, len(P))
        p = hb.guess(self.W, E, v)
        T = fine(P)
        D = self.cb @ I16
        code = bits[:, 0] * 2 + bits[:, 1]
        for g in np.where(code == 0)[0]:
            ts = np.where((f == g) & uu)[0]
            if ceil[g] is None or not len(ts):
                continue
            G = ceil_gain_db(FC, ceil[g])
            err = np.zeros(len(self.cb))
            for t in ts:
                fl = T[t].max() - 40
                err += ((np.maximum((p[t] @ I16)[None] + D + G, fl) - np.maximum(T[t], fl)) ** 2).sum(1)
            i = int(np.argmin(err))
            bits[g, 2:2 + self.nb] = [(i >> (self.nb - 1 - k)) & 1 for k in range(self.nb)]

    # ------------------------------------------------------------ decoder
    def frame_ceiling(self, bits, good=None):
        """Per codec frame, the ceiling in force: from the frame that completes a ceiling
        message (16 side bits in a window's UU frames; a lost frame before that drops it)."""
        good = np.ones(len(bits), bool) if good is None else np.asarray(good, bool)
        cur, out = None, [None] * len(bits)
        for g0 in range(0, len(bits), hb.WIN_FRAMES):
            got, ok = [], True
            for g in range(g0, min(len(bits), g0 + hb.WIN_FRAMES)):
                out[g] = cur
                if not ok or len(got) == hb.MSG_BITS:
                    continue
                if not good[g]:
                    ok = False
                elif bits[g, 0] == 0 and bits[g, 1] == 0:
                    got.append(int(bits[g, 8]))
                    if len(got) == hb.MSG_BITS:
                        c = unpack_ceiling(got)
                        if c is not False:
                            cur = out[g] = c
        return out

    decode_audio16 = _ns["decode_audio16"]

    def _post_hiss(self, X, y16, b2, bits, good, f, nfr):
        if not nfr:
            return X
        ceil = self.frame_ceiling(np.asarray(bits), good) if self.ceiling else [None] * len(b2)
        cf = [ceil[min(g, len(ceil) - 1)] for g in f]
        gain = np.zeros(X.shape)
        for t, c in enumerate(cf):
            if c is not None:
                gain[t] = ceil_gain_db(hb.FR, c)
        if self.cap:
            Y = np.abs(hb.stft(y16)[:nfr]) ** 2
            hi = 10 * np.log10(((np.abs(X) ** 2 + Y)[:, hb.FR >= 4000]).mean(1) + 1e-12)
            mid = 10 * np.log10(Y[:, (hb.FR >= 2000) & (hb.FR < 4000)].mean(1) + 1e-12)
            code = b2[:, 0] * 2 + b2[:, 1]
            nons = (code[f] != 1) & (hi < mid - CAP_MARGIN) & np.array([c is None for c in cf])
            w = np.convolve(nons.astype(float), np.ones(3) / 3, "same")
            gain += w[:, None] * -np.clip((hb.FR - CAP_FC) * CAP_S / 1000.0, 0.0, CAP_DEPTH)[None, :]
        return X * 10 ** (gain / 20)
