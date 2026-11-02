"""14-bit hiss: v3.8's 6-bit 4-band index + an 8-bit 16-bin refinement stage. Oracle eval on
the test clips' s frames (smoothed truth), like vq16.py."""
import sys, os, glob, numpy as np, soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling as cl; from measure import ENC, vl, hb
from vq16 import kmeans, I, T, p, sib, cb4
vl.TRACK2['lag'] = 8
r4 = (T @ np.linalg.pinv(I)) if False else None
# per training frame: 4-band residual target = band levels of T minus guess; T is 16 bins -> 4 bands by power mean
B4 = 10*np.log10(np.stack([np.mean(10**(T[:, 4*b:4*b+4]/10), 1) for b in range(4)], 1))
i4 = np.argmin(((B4 - p)[:, None, :] - cb4[None]) ** 2, axis=2).sum if False else None
D = ((B4 - p)[:, None, :] - cb4[None]) ** 2
i4 = D.sum(2).argmin(1)
R2 = np.clip(T - (p + cb4[i4]) @ I, -45, 45)
w = np.where(sib, 3.0, 1.0)
if os.path.exists('/tmp/claude-0/sib/cb2.npy'): CB2 = np.load('/tmp/claude-0/sib/cb2.npy')
else:
    CB2 = kmeans(R2, 256, w, it=25); np.save('/tmp/claude-0/sib/cb2.npy', CB2)
def con(d, k): mu = 10*np.log10(np.mean(10**(d/10))); return mu + k*(d - mu)
rows = []
for path in [q for q in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')) if '3170' not in q and '777' not in q]:
    x = sf.read(path)[0]; x = x[:len(x)//320*320]
    e = hb.SottoV38(**ENC); bits, hbands, m = e.encode_low(x)
    P = np.abs(hb.stft(x)) ** 2
    E, v, fr, uu = hb.frame_map(m, bits, len(P)); pg = hb.guess(e.W, E, v)
    s = cl.sib_frames(P) & uu; Tn = cl.fine(P)
    Psm = np.stack([np.convolve(P[:, j], np.ones(3) / 3, 'same') for j in range(P.shape[1])], 1); Ts = cl.fine(Psm)
    out = {}
    for g in np.unique(fr[s]):
        ts = np.where((fr == g) & uu)[0]
        res4 = (hbands[ts] - pg[ts]).mean(0); a = np.argmin(((cb4 - res4) ** 2).sum(1))
        r2 = (Tn[ts] - (pg[ts] + cb4[a]) @ I).mean(0); b = np.argmin(((CB2 - r2) ** 2).sum(1))
        for t in ts:
            if not s[t]: continue
            d4 = (pg[t] + cb4[a]) @ I; d14 = d4 + CB2[b]
            for k, d in (('6-bit (v3.8)', d4), ('6+8 bits', d14), ('true 16 bins', Tn[t])):
                fl = Ts[t].max() - 40
                nl = lambda q: q - 10 * np.log10(np.mean(10 ** (q / 10)))
                sh = np.maximum(nl(d), nl(Ts[t]).max() - 40) - np.maximum(nl(Ts[t]), nl(Ts[t]).max() - 40)
                prom = lambda q: q.max() - 10 * np.log10(np.mean(10 ** (q / 10)))
                out.setdefault(k, []).append((np.sqrt((sh ** 2).mean()), prom(d), prom(Ts[t]), abs(cl.FC[np.argmax(d)] - cl.FC[np.argmax(Ts[t])])))
    rows.append({k: np.mean(v, 0) for k, v in out.items()})
    print(os.path.basename(path)[:-4].ljust(22), ' | '.join(f'{k}: shape {q[0]:4.1f} prom {q[1]:3.1f}/{q[2]:3.1f} pk miss {q[3]:4.0f}' for k, q in rows[-1].items()), flush=True)
print('MEAN', ' | '.join(f'{k}: ' + ' '.join(f'{q:.2f}' for q in np.mean([r[k] for r in rows], 0)) for k in rows[0]))
