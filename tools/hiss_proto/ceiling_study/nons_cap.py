"""Jeff's idea (2026-11-02): the non-s fricatives (sh, f, th, z) want a band-limited hiss.
Decoder-only rule on top of v3.8: an unvoiced, mixed or edge frame whose decoded 4-8 kHz
level is more than `margin` dB under its decoded 2-4 kHz level is treated as a non-s
fricative, and its high band gets -min(S (f - fc), 40) dB. The per-frame decision is smoothed
over 3 frames. s frames (4-8 kHz at or above 2-4 kHz) and vowels are untouched. No bits."""
import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling as cl
hb = cl.hb


def cap_nons(y, bits, fc=5000.0, S=24.0, margin=3.0, depth=40.0):
    """-> (capped y, fraction of STFT frames capped)"""
    X = hb.stft(np.pad(y, (hb.H, hb.N)))
    P = np.abs(X) ** 2
    hi = hb.bands_db(P, [(4000, 8000)])[:, 0]; mid = hb.bands_db(P, [(2000, 4000)])[:, 0]
    code = bits[:, 0] * 2 + bits[:, 1]
    t = np.arange(len(X)); g = np.clip(t - 1 + hb.HB_LAG, 0, 2 * len(bits) - 1) // 2
    nons = (code[g] != 1) & (hi < mid - margin)
    w = np.convolve(nons.astype(float), np.ones(3) / 3, 'same')
    G = -np.clip((hb.FR - fc) * S / 1000, 0, depth)
    X *= 10 ** (w[:, None] * G[None, :] / 20)
    return hb.istft(X, len(y) + hb.H + hb.N)[hb.H:hb.H + len(y)], float(nons.mean())
