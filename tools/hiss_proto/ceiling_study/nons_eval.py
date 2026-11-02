import sys, glob, os, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from measure import run, hb; import nons_cap as nc
B = [(2000, 3000), (3000, 4000), (4000, 5000), (5000, 6000), (6000, 7000), (7000, 8000)]
V = {'v3.8': None, 'soft (5 kHz, 24 dB/kHz)': (5000, 24), 'soft, z exempt 0': (5000, 24, 3.0, 40.0, 0.0), 'soft, z exempt -6': (5000, 24, 3.0, 40.0, -6.0), 'soft, z exempt +6': (5000, 24, 3.0, 40.0, 6.0)}
acc = {}
for p in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    if '777' in p or '3170' in p: continue
    x, b, y0 = run(p); y0 = y0[:len(x)]
    P = np.abs(hb.stft(x)) ** 2; n = len(P)
    db = lambda M, a, e: 10 * np.log10(M[:, (hb.FR >= a) & (hb.FR < e)].mean(1) + 1e-12)
    tot = 10 * np.log10(P.sum(1) + 1e-12); lo, mid, hi = db(P, 0, 1000), db(P, 2000, 4000), db(P, 4000, 8000)
    code = b[:, 0] * 2 + b[:, 1]; fr = np.clip(np.arange(n) + hb.HB_LAG, 0, 2 * len(b) - 1) // 2; c = code[fr]
    pk, fl = np.percentile(tot, 97), np.percentile(tot, 10); live = tot > fl + 10
    K = {'s': live & (hi > lo + 6) & (hi > mid), 'sh': live & (mid > hi + 3) & (mid > lo) & (c == 0),
         'f/th': live & (c == 0) & (tot < pk - 20) & (np.abs(hi - mid) < 6) & (hi < lo + 6) & (mid > lo - 6),
         'z/v/zh': live & (c != 0) & (hi > lo - 15), 'vowels': live & (c == 1) & (hi < lo - 25) & (tot > pk - 15)}
    for vn, cfg in V.items():
        y = y0 if cfg is None else nc.cap_nons(y0, b, *cfg)[0]
        Q = np.abs(hb.stft(y)) ** 2; Q = Q[:n]
        for k, s in K.items():
            if s.sum() < 3: continue
            d = [10 * np.log10(Q[s][:, (hb.FR >= a) & (hb.FR < e)].mean() + 1e-12) - 10 * np.log10(P[s][:, (hb.FR >= a) & (hb.FR < e)].mean() + 1e-12) for a, e in B]
            acc.setdefault((vn, k), []).append(d)
print(' ' * 36 + ' '.join(f'{a/1000:.0f}-{e/1000:.0f}k' for a, e in B))
for (vn, k), v in acc.items():
    print(f'{vn:26s} {k:8s}' + ' '.join(f'{z:+5.1f}' for z in np.mean(v, 0)))
