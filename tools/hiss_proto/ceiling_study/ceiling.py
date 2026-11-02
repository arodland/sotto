"""Per-speaker hiss ceiling: the 4-8 kHz hiss above a cutoff fc falls at S dB/kHz.
Encoder: measures (fc, S) on the speaker's sibilant frames, and searches each UU frame's hiss
index against the 250 Hz-bin spectrum the decoder will make (interpolation + ceiling).
Decoder: one gain per FFT bin on the whole 4-8 kHz layer."""
import os, sys, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "sim"))
import vocoder_lab as vl, hiss_band as hb
FCS = 4250 + 250*np.arange(15)            # 4.25..7.75 kHz, code 15 = off
SLOPES = np.array([12.0, 24.0, 48.0, 96.0])  # dB/kHz
FLOOR = 60.0
FB = np.arange(4000, 8000, 250); FC = FB + 125

def gain_db(f, fc, S):
    if fc is None: return np.zeros_like(f, float)
    return -np.clip((f - fc) * S / 1000, 0, FLOOR)

def fine(P):
    return np.stack([10*np.log10(P[..., (hb.FR >= a) & (hb.FR < a+250)].mean(-1)+1e-12) for a in FB], -1)

def sib_frames(P):
    tot = 10*np.log10(P.sum(1)+1e-12); lo = hb.bands_db(P, [(0, 1000)])[:, 0]; hi = hb.bands_db(P, [(4000, 8000)])[:, 0]
    return (tot > np.percentile(tot, 97)-30) & (hi > lo+6)

def measure(x16, min_gain=3.0):
    """-> (fc, S) or None. Mean sibilant spectrum (each frame re its 4-6 kHz level), fit
    a + b f + G(f) per (fc, S) with both sides floored 50 dB below the peak; take the ceiling
    only if it cuts the rms misfit by min_gain dB vs no ceiling."""
    P = np.abs(hb.stft(x16))**2
    s = sib_frames(P)
    if s.sum() < 10: return None
    F = fine(P[s]); F -= 10*np.log10(np.mean(10**(F[:, :8]/10), 1))[:, None]
    m = 10*np.log10(np.mean(10**(F/10), 0))
    fl = m.max() - 50
    mc = np.maximum(m, fl); f = FC/1000.0
    def fit(G):
        w = (G > -1)                               # fit the line on the passband only
        if w.sum() < 2: return 1e9
        a, b = np.polyfit(f[w], m[w], 1)[::-1] if w.sum() >= 3 else (m[w].mean(), 0.0)
        e = np.maximum(a + b*f + G, fl) - mc
        return np.sqrt((e**2).mean())
    base = fit(np.zeros(len(f)))
    best = min(((fit(gain_db(FC, fc, S)), fc, S) for fc in FCS for S in SLOPES))
    return (best[1], best[2]) if base - best[0] >= min_gain else None

class SottoV38C(hb.SottoV38):
    def __init__(self, ceiling=None, abs_search=True, **kw):
        super().__init__(**kw); self.ceiling, self.abs_search = ceiling, abs_search

    def encode(self, x16):
        bits = super().encode(x16)
        if self.ceiling is None or not self.abs_search: return bits
        # redo the UU hiss index: best match in 250 Hz bins after the ceiling
        fc, S = self.ceiling
        _, hbands, m = self.encode_low(x16)
        P = np.abs(hb.stft(x16))**2
        E, v, f, uu = hb.frame_map(m, bits, len(P))
        p = hb.guess(self.W, E, v)
        T = fine(P)
        Gb = gain_db(FC, fc, S)
        # interpolation of a 4-band vector onto the 16 bin centres (linear in dB)
        I = np.stack([np.interp(FC, hb.HBC, e) for e in np.eye(4)], 0)    # 4 x 16
        Dcb = self.cb @ I                                                   # 64 x 16
        nb = self.nb
        code = bits[:, 0]*2 + bits[:, 1]
        for g in np.where(code == 0)[0]:
            ts = np.where((f == g) & uu)[0]
            if not len(ts): continue
            err = np.zeros(len(self.cb))
            for t in ts:
                tgt = T[t]; fl = tgt.max() - 40
                dec = (p[t] @ I)[None, :] + Dcb + Gb[None, :]
                err += ((np.maximum(dec, fl) - np.maximum(tgt, fl))**2).sum(1)
            i = int(np.argmin(err))
            old = bits[g, 2:2+nb].copy()
            bits[g, 2:2+nb] = [(i >> (nb-1-b)) & 1 for b in range(nb)]
        return bits

    def decode_audio16(self, bits, good=None, n16=None, seed=0):
        y = super().decode_audio16(bits, good, n16, seed)
        if self.ceiling is None: return y
        fc, S = self.ceiling
        X = hb.stft(np.pad(y, (hb.H, hb.N)))
        X *= 10**(gain_db(hb.FR, fc, S)/20)[None, :]
        return hb.istft(X, len(y) + hb.H + hb.N)[hb.H:hb.H+len(y)]
