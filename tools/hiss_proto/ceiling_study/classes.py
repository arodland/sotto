"""Decoded minus original high-band level, per kind of sound (v3.8 candidate, clean)."""
import sys, glob, os, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from measure import run, hb
B = [(1000, 2000), (2000, 3000), (3000, 4000), (4000, 5000), (5000, 6000), (6000, 7000), (7000, 8000)]
acc = {}
for p in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    if '777' in p or '3170' in p: continue        # MP3 cut-offs: ceiling cases, measured before
    x, b, y = run(p)
    P = np.abs(hb.stft(x)) ** 2; Q = np.abs(hb.stft(y[:len(x)])) ** 2; n = min(len(P), len(Q)); P, Q = P[:n], Q[:n]
    db = lambda M, a, e: 10 * np.log10(M[:, (hb.FR >= a) & (hb.FR < e)].mean(1) + 1e-12)
    tot = 10 * np.log10(P.sum(1) + 1e-12); lo, mid, hi = db(P, 0, 1000), db(P, 2000, 4000), db(P, 4000, 8000)
    code = b[:, 0] * 2 + b[:, 1]; fr = np.clip(np.arange(n) + hb.HB_LAG, 0, 2 * len(b) - 1) // 2; c = code[fr]
    pk, fl = np.percentile(tot, 97), np.percentile(tot, 10)
    live = tot > fl + 10
    jump = np.r_[False, hi[1:] - hi[:-1] > 12]
    K = {
        's (4-8 kHz dominant)': live & (hi > lo + 6) & (hi > mid),
        'sh (2-4 kHz dominant)': live & (mid > hi + 3) & (mid > lo) & (c == 0),
        'f/th (weak, flat, unvoiced)': live & (c == 0) & (tot < pk - 20) & (np.abs(hi - mid) < 6) & (hi < lo + 6) & (mid > lo - 6),
        'z/v/zh (voiced or mixed, strong 4-8 kHz)': live & (c != 0) & (hi > lo - 15),
        't/k/p bursts (4-8 kHz jumps 12 dB)': live & jump,
        'vowels (voiced, weak 4-8 kHz)': live & (c == 1) & (hi < lo - 25) & (tot > pk - 15),
        'pauses / background': (tot < fl + 6),
    }
    for k, s in K.items():
        if s.sum() < 3: continue
        d = [10 * np.log10(Q[s][:, (hb.FR >= a) & (hb.FR < e)].mean() + 1e-12) - 10 * np.log10(P[s][:, (hb.FR >= a) & (hb.FR < e)].mean() + 1e-12) for a, e in B]
        acc.setdefault(k, []).append((s.sum(), d))
print('decoded - original, dB (power over each class, then mean over clips)')
print(' ' * 42 + ' '.join(f'{a/1000:.0f}-{e/1000:.0f}k' for a, e in B), '  frames  clips')
for k, v in acc.items():
    d = np.mean([q[1] for q in v], 0)
    print(f'{k:42s}' + ' '.join(f'{z:+5.1f}' for z in d), f'{sum(q[0] for q in v):7d} {len(v):5d}')
