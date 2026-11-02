"""v3.8 vs v3.8 + ceiling + 14-bit s shape: PESQ and the s frames' 4-8 kHz shape after decoding."""
import os, sys, glob, numpy as np, soundfile as sf
from scipy.signal import resample_poly
from pesq import pesq
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling as cl, s_model as sm
from measure import ENC, DEC, run, vl, hb
vl.TRACK2['lag'] = 8
C = os.environ.get('CLIPS16', '/tmp/claude-0/hiss/clips16')
OUT = os.environ.get('OUT', '/tmp/claude-0/sib/s14'); os.makedirs(OUT, exist_ok=True)
def sstats(x, y, b):
    P = np.abs(hb.stft(x)) ** 2; Q = np.abs(hb.stft(y[:len(x)])) ** 2; n = min(len(P), len(Q)); P, Q = P[:n], Q[:n]
    code = b[:, 0] * 2 + b[:, 1]; fr = np.clip(np.arange(n) + hb.HB_LAG, 0, 2 * len(b) - 1) // 2
    s = cl.sib_frames(P) & (code[fr] == 0)
    sm3 = lambda M: np.stack([np.convolve(M[:, j], np.ones(3) / 3, 'same') for j in range(M.shape[1])], 1)
    To, Td = cl.fine(sm3(P))[s], cl.fine(sm3(Q))[s]
    nl = lambda F: F - 10 * np.log10(np.mean(10 ** (F / 10), 1))[:, None]
    fl = nl(To).max(1, keepdims=True) - 40
    sh = np.sqrt(((np.maximum(nl(Td), fl) - np.maximum(nl(To), fl)) ** 2).mean())
    prom = lambda F: (F.max(1) - 10 * np.log10(np.mean(10 ** (F / 10), 1))).mean()
    return sh, prom(Td), prom(To)
rows = []
for p in sorted(glob.glob(f'{C}/*.wav')):
    k = os.path.basename(p)[:-4]
    x, b0, y0 = run(p)
    if os.path.exists(f'{OUT}/{k}_s14r.npy'):
        b1, y1, y2 = (np.load(f'{OUT}/{k}_{s}.npy') for s in ('bits', 's14', 's14r'))
    else:
      e = sm.SottoS14(**ENC); b1 = e.encode(x)
      y1 = sm.SottoS14(**DEC).decode_audio16(b1, n16=len(x))
      y2 = sm.SottoS14(rayleigh=True, **DEC).decode_audio16(b1, n16=len(x))
      np.save(f'{OUT}/{k}_s14.npy', y1); np.save(f'{OUT}/{k}_s14r.npy', y2); np.save(f'{OUT}/{k}_bits.npy', b1)
    x8 = resample_poly(x, 1, 2); r = [k]
    for y, b in ((y0, b0), (y1, b1), (y2, b1)):
        n = min(len(x), len(y))
        r += [pesq(8000, x8[:n // 2], resample_poly(y[:n], 1, 2)[:n // 2], 'nb'), pesq(16000, x[:n], y[:n], 'wb'), *sstats(x, y, b)]
    rows.append(r)
    print(f'{k:22s} v3.8 NB {r[1]:.3f} WB {r[2]:.3f} s-shape {r[3]:.1f} prom {r[4]:.1f}/{r[5]:.1f} | s14 NB {r[6]:.3f} WB {r[7]:.3f} s-shape {r[8]:.1f} prom {r[9]:.1f} | +rayleigh NB {r[11]:.3f} WB {r[12]:.3f} s-shape {r[13]:.1f} prom {r[14]:.1f}', flush=True)
M = np.mean([r[1:] for r in rows], 0)
print('MEAN v3.8 NB %.3f WB %.3f shape %.2f prom %.2f/%.2f | s14 NB %.3f WB %.3f shape %.2f prom %.2f | rayleigh NB %.3f WB %.3f shape %.2f prom %.2f' % (M[0], M[1], M[2], M[3], M[4], M[5], M[6], M[7], M[8], M[10], M[11], M[12], M[13]))
