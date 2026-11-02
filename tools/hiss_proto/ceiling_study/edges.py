"""4-8 kHz level around fricative onsets and offsets, 5 ms steps: original vs v3.8."""
import os, sys, glob, numpy as np
from scipy.signal import butter, sosfiltfilt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from measure import run, hb
def env(z, lo, hi):
    e = sosfiltfilt(butter(6, [lo, hi], 'band', fs=16000, output='sos'), z) ** 2
    return np.add.reduceat(e, np.arange(0, len(e), 80)) / 80          # 5 ms power
OFF = np.arange(-12, 13)                                               # -60..+60 ms
on_o, on_d, of_o, of_d, lo_on_o, lo_on_d, lo_of_o, lo_of_d = ([] for _ in range(8))
for p in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    if '777' in p or '3170' in p: continue
    x, b, y = run(p); y = y[:len(x)]
    P = np.abs(hb.stft(x)) ** 2
    db = lambda a, e: 10 * np.log10(P[:, (hb.FR >= a) & (hb.FR < e)].mean(1) + 1e-12)
    tot = 10 * np.log10(P.sum(1) + 1e-12); lo, hi = db(0, 1000), db(4000, 8000)
    fric = (hi > lo) & (tot > np.percentile(tot, 10) + 10)            # any frication-dominated frame
    hx, hy = env(x, 4000, 7900), env(y, 4000, 7900); mx, my = env(x, 1000, 3900), env(y, 1000, 3900)
    # runs of >= 3 fricative frames (10 ms STFT hop; frame t centre = t*160+160 samples -> 5 ms index 2t+2)
    t = 0
    while t < len(fric):
        if not fric[t]: t += 1; continue
        u = t
        while u < len(fric) and fric[u]: u += 1
        if u - t >= 3:
            for k, L in ((2 * t + 1, (on_o, on_d, lo_on_o, lo_on_d)), (2 * u + 1, (of_o, of_d, lo_of_o, lo_of_d))):
                idx = k + OFF
                if idx[0] >= 0 and idx[-1] < min(len(hx), len(hy)):
                    L[0].append(hx[idx]); L[1].append(hy[idx]); L[2].append(mx[idx]); L[3].append(my[idx])
        t = u
dB = lambda L: 10 * np.log10(np.mean(L, 0) + 1e-20)
print(f'{len(on_o)} fricative runs. dB re the original 4-8 kHz level at the middle of the run edge')
for nm, o, d, mo, md in (('ONSET', on_o, on_d, lo_on_o, lo_on_d), ('OFFSET', of_o, of_d, lo_of_o, lo_of_d)):
    ref = dB(o)[12]
    print(nm, 'ms      ', ' '.join(f'{5*k:+4d}' for k in OFF[::2]))
    print('  4-8k orig', ' '.join(f'{v:+4.0f}' for v in (dB(o) - ref)[::2]))
    print('  4-8k v3.8', ' '.join(f'{v:+4.0f}' for v in (dB(d) - ref)[::2]))
    print('  1-4k orig', ' '.join(f'{v:+4.0f}' for v in (dB(mo) - ref)[::2]))
    print('  1-4k v3.8', ' '.join(f'{v:+4.0f}' for v in (dB(md) - ref)[::2]))
