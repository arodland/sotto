"""Hiss ceiling over the air: the (cut-off, steepness) pair rides the v3.8 side channel.

A ceiling message is a 16-bit side-channel message like the vowel-shape one (12 payload +
CRC-4) whose CRC is XORed with CEIL_MASK. CRC-4 is affine, so a valid vowel message never
passes the ceiling check and vice versa: the type costs no bits, and a v3.8 decoder just sees
a failed message and keeps its vowel shape. Payload: cut-off code (4 bits, 0-14 = 4.25-7.75
kHz, 15 = off), steepness code (2 bits), 6 spare bits (0).

Encoder (causal): sibilant spectra of windows 0..w-1 are accumulated; from 10 sibilant frames
on, the ceiling is re-measured each window. Window w sends a ceiling message when the decision
changed, and then again every REPEAT windows while a ceiling is on; every other window carries
the vowel shape as in v3.8. A voice with no ceiling never sends one: its bitstream is v3.8.
Decoder: the ceiling applies from the frame that completes its message."""
import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling as cl
hb = cl.hb
CEIL_MASK = [1, 0, 1, 0]
REPEAT = 5
MIN_SIB = 10


def pack_ceiling(c):
    fi = 15 if c is None else int(np.argmin(np.abs(cl.FCS - c[0])))
    si = 0 if c is None else int(np.argmin(np.abs(cl.SLOPES - c[1])))
    b = [(fi >> (3 - i)) & 1 for i in range(4)] + [(si >> (1 - i)) & 1 for i in range(2)] + [0] * 6
    return b + [x ^ m for x, m in zip(hb.crc4(b), CEIL_MASK)]


def unpack_ceiling(b):
    if len(b) < 16 or [x ^ m for x, m in zip(hb.crc4(b[:12]), CEIL_MASK)] != list(b[12:16]):
        return False                                 # not a ceiling message
    fi = int("".join(map(str, b[0:4])), 2); si = int("".join(map(str, b[4:6])), 2)
    return None if fi == 15 else (float(cl.FCS[fi]), float(cl.SLOPES[si]))


def measure_spectra(Fsum, n, fl_db=50.0, min_gain=3.0):
    """ceiling.measure() on an accumulated mean spectrum (16 bins, power)."""
    if n < MIN_SIB: return None
    m = 10 * np.log10(Fsum / n + 1e-20)
    fl = m.max() - fl_db; mc = np.maximum(m, fl); f = cl.FC / 1000.0
    def fit(G):
        w = G > -1
        if w.sum() < 2: return 1e9
        a, b = np.polyfit(f[w], m[w], 1)[::-1] if w.sum() >= 3 else (m[w].mean(), 0.0)
        return np.sqrt(((np.maximum(a + b * f + G, fl) - mc) ** 2).mean())
    base = fit(np.zeros(len(f)))
    best = min((fit(cl.gain_db(cl.FC, fc, S)), fc, S) for fc in cl.FCS for S in cl.SLOPES)
    return (float(best[1]), float(best[2])) if base - best[0] >= min_gain else None


class SottoV38S(cl.SottoV38C):
    """vowel="speaker" v3.8 with the ceiling sent in the side channel (see module doc)."""

    def __init__(self, **kw):
        super().__init__(ceiling=None, **kw)

    # ------------------------------------------------------------ encoder
    def _ceiling_schedule(self, x16, nwin):
        """-> per window: ceiling decision known before it (None/(fc, S)), and whether it sends."""
        P = np.abs(hb.stft(x16)) ** 2
        nfr = len(P)
        tot = 10 * np.log10(P.sum(1) + 1e-12)
        lo = hb.bands_db(P, [(0, 1000)])[:, 0]; hi = hb.bands_db(P, [(4000, 8000)])[:, 0]
        loud = hb.running_pct(tot, 50, warm=50)       # causal stand-in for "within 30 dB of the 97th pct"
        sib = loud & (hi > lo + 6)
        F = cl.fine(P)
        F = 10 ** ((F - 10 * np.log10(np.mean(10 ** (F[:, :8] / 10), 1))[:, None]) / 10)
        fr = np.clip(np.arange(nfr) + hb.HB_LAG, 0, 2 * self._nbits - 1) // 2
        acc, n, cur, last_sent, out = np.zeros(16), 0, None, -99, []
        for w in range(nwin):
            dec = measure_spectra(acc, n)
            send = (dec != cur) or (dec is not None and w - last_sent >= REPEAT)
            if send: last_sent = w
            cur = dec
            out.append((cur, send))
            ts = np.where((fr >= w * hb.WIN_FRAMES) & (fr < (w + 1) * hb.WIN_FRAMES) & sib)[0]
            acc += F[ts].sum(0); n += len(ts)
        return out

    def encode(self, x16):
        self._nbits = len(x16) // 160
        self._sched = None
        bits = hb.SottoV38.encode(self, x16)             # vowel side channel via _side_pc below
        # hiss index search against the spectrum after the ceiling in force at each frame
        ceil = self._frame_ceiling(bits)
        if any(c is not None for c in ceil):
            self._abs_search(bits, x16, ceil)
        return bits

    def _side_pc(self, bits, x16):
        hb.SottoV38._side_pc(self, bits, x16)           # writes the vowel messages
        nwin = (len(bits) + hb.WIN_FRAMES - 1) // hb.WIN_FRAMES
        self._sched = self._ceiling_schedule(x16, nwin)
        code = bits[:, 0] * 2 + bits[:, 1]
        for w, (c, send) in enumerate(self._sched):
            if not send: continue
            uuf = [g for g in range(w * hb.WIN_FRAMES, min(len(bits), (w + 1) * hb.WIN_FRAMES)) if code[g] == 0]
            for g, b in zip(uuf, pack_ceiling(c)):
                bits[g, 8] = b

    def _frame_ceiling(self, bits, good=None):
        """Decoder view: per codec frame, the ceiling in force (from the frame completing a message)."""
        good = np.ones(len(bits), bool) if good is None else good
        cur, out = None, [None] * len(bits)
        for g0 in range(0, len(bits), hb.WIN_FRAMES):
            got, ok = [], True
            for g in range(g0, min(len(bits), g0 + hb.WIN_FRAMES)):
                out[g] = cur
                if not ok or len(got) == hb.MSG_BITS: continue
                if not good[g]: ok = False
                elif bits[g, 0] == 0 and bits[g, 1] == 0:
                    got.append(int(bits[g, 8]))
                    if len(got) == hb.MSG_BITS:
                        c = unpack_ceiling(got)
                        if c is not False:
                            cur = c; out[g] = cur
        return out

    def _abs_search(self, bits, x16, ceil):
        _, _, m = self.encode_low(x16)
        P = np.abs(hb.stft(x16)) ** 2
        E, v, f, uu = hb.frame_map(m, bits, len(P))
        p = hb.guess(self.W, E, v)
        T = cl.fine(P)
        I = np.stack([np.interp(cl.FC, hb.HBC, e) for e in np.eye(4)], 0)
        Dcb = self.cb @ I
        nb = self.nb
        code = bits[:, 0] * 2 + bits[:, 1]
        for g in np.where(code == 0)[0]:
            if ceil[g] is None: continue
            ts = np.where((f == g) & uu)[0]
            if not len(ts): continue
            Gb = cl.gain_db(cl.FC, *ceil[g])
            err = np.zeros(len(self.cb))
            for t in ts:
                tgt = T[t]; fl = tgt.max() - 40
                dec = (p[t] @ I)[None, :] + Dcb + Gb[None, :]
                err += ((np.maximum(dec, fl) - np.maximum(tgt, fl)) ** 2).sum(1)
            i = int(np.argmin(err))
            bits[g, 2:2 + nb] = [(i >> (nb - 1 - b)) & 1 for b in range(nb)]   # bit 8 (side) is outside 2..7

    # ------------------------------------------------------------ decoder
    def decode_audio16(self, bits, good=None, n16=None, seed=0):
        y = hb.SottoV38.decode_audio16(self, bits, good, n16, seed)
        ceil = self._frame_ceiling(np.asarray(bits), None if good is None else np.asarray(good, bool))
        if all(c is None for c in ceil): return y
        X = hb.stft(np.pad(y, (hb.H, hb.N)))
        for t in range(len(X)):
            g = min(max(t - 1 + hb.HB_LAG, 0) // 2, len(ceil) - 1)
            if ceil[g] is not None:
                X[t] *= 10 ** (cl.gain_db(hb.FR, *ceil[g]) / 20)
        return hb.istft(X, len(y) + hb.H + hb.N)[hb.H:hb.H + len(y)]
