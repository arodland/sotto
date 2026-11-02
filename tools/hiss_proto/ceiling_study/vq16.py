"""16-bin (250 Hz) hiss codebook vs the 4-band one, same 6 bits. Train on LibriSpeech training
speakers' UU frames (k-means in dB on the residual from the interpolated guess), evaluate on the
test clips' sibilant frames against a 3-frame-smoothed truth."""
import sys, os, glob, numpy as np, soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling as cl; from measure import ENC, vl, hb
vl.TRACK2['lag'] = 8
I = np.stack([np.interp(cl.FC, hb.HBC, e) for e in np.eye(4)], 0)        # 4 -> 16 bins
Z = [np.load(f) for f in sorted(glob.glob('/tmp/claude-0/sib/train_*.npz'))]
T = np.concatenate([z['T'] for z in Z]); p = np.concatenate([z['p'] for z in Z]); sib = np.concatenate([z['sib'] for z in Z])
R = np.clip(T - p @ I, -45, 45)
print('training frames', len(R), 'sibilant', sib.sum())
def kmeans(X, k, w=None, it=40, seed=0):
    rng = np.random.default_rng(seed); w = np.ones(len(X)) if w is None else w
    C = X[rng.choice(len(X), k, replace=False)]
    for _ in range(it):
        d = ((X[:, None, :] - C[None]) ** 2).sum(2); a = d.argmin(1)
        for j in range(k):
            s = a == j
            C[j] = (X[s] * w[s, None]).sum(0) / w[s].sum() if s.any() else X[rng.integers(len(X))]
    return C
w = np.where(sib, 3.0, 1.0)                                    # weight the s frames
CB16 = kmeans(R, 64, w)
np.save('/tmp/claude-0/sib/cb16.npy', CB16)
cb4 = np.load('/home/user/sotto/sim/data/hiss_band.npz', allow_pickle=True)['cb6']
def evaluate(paths):
    rows = []
    for path in paths:
        x = sf.read(path)[0]; x = x[:len(x)//320*320]
        e = hb.SottoV38(**ENC); bits, hbands, m = e.encode_low(x)
        P = np.abs(hb.stft(x)) ** 2
        E, v, fr, uu = hb.frame_map(m, bits, len(P)); pg = hb.guess(e.W, E, v)
        s = cl.sib_frames(P) & uu
        Tn = cl.fine(P)
        Ps = np.convolve(np.ones(3), [1], 'full')                        # 3-frame power smoothing
        Psm = np.stack([np.convolve(P[:, j], np.ones(3) / 3, 'same') for j in range(P.shape[1])], 1)
        Ts = cl.fine(Psm)
        out = {}
        for g in np.unique(fr[s]):
            ts = np.where((fr == g) & uu)[0]
            res4 = (hbands[ts] - pg[ts]).mean(0); i4 = np.argmin(((cb4 - res4) ** 2).sum(1))
            r16 = (Tn[ts] - pg[ts] @ I).mean(0); i16 = np.argmin(((CB16 - r16) ** 2).sum(1))
            for t in ts:
                if not s[t]: continue
                d4 = (pg[t] + cb4[i4]) @ I; d16 = pg[t] @ I + CB16[i16]
                for k, d in (('4-band', d4), ('16-bin', d16), ('true 4 bands', hbands[t] @ I)):
                    fl = Ts[t].max() - 40
                    e_ = np.maximum(d, fl) - np.maximum(Ts[t], fl)
                    nl = lambda q: q - 10 * np.log10(np.mean(10 ** (q / 10)))
                    sh = np.maximum(nl(d), nl(Ts[t]).max() - 40) - np.maximum(nl(Ts[t]), nl(Ts[t]).max() - 40)
                    prom = lambda q: q.max() - 10 * np.log10(np.mean(10 ** (q / 10)))
                    pk = lambda q: cl.FC[np.argmax(q)]
                    out.setdefault(k, []).append((np.sqrt((e_ ** 2).mean()), np.sqrt((sh ** 2).mean()), prom(d), prom(Ts[t]), abs(pk(d) - pk(Ts[t]))))
        print(os.path.basename(path)[:-4].ljust(22), ' | '.join(f'{k}: rms {np.mean([r[0] for r in v]):4.1f} shape {np.mean([r[1] for r in v]):4.1f} prom {np.mean([r[2] for r in v]):3.1f}/{np.mean([r[3] for r in v]):3.1f} pk miss {np.mean([r[4] for r in v]):4.0f}' for k, v in out.items()), flush=True)
        rows.append({k: np.mean(v, 0) for k, v in out.items()})
    print('MEAN', ' | '.join(f'{k}: ' + ' '.join(f'{q:.2f}' for q in np.mean([r[k] for r in rows], 0)) for k in rows[0]))
if __name__ == '__main__':
    evaluate([p for p in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')) if '3170' not in p and '777' not in p])
