"""Vowel high band for bright voices: how well can V/M frames' 4-8 kHz be set from
(a) the guess, (b) guess + a per-speaker offset sent slowly (causal running mean of the
encoder's residuals, quantized to 2 dB, refreshed every R seconds), (c) the oracle
per-speaker mean. Also the cost of shrinking the UU index from 7 to 6 bits."""
import sys, glob, os, pickle
sys.path.insert(0, 'sim'); sys.path.insert(0, 'tools')
import numpy as np, soundfile as sf
import vocoder_lab as vl, hiss_band as hb
import train_hiss_band as th
from multiprocessing import Pool
from sklearn.cluster import KMeans
vl.TRACK2["lag"] = 8
D = '/tmp/claude-0/data/ls/LibriSpeech/dev-clean'
def te_one(c):
    vl.TRACK2["lag"] = 8
    x = sf.read(c)[0]; x = x[:len(x)//320*320]
    bits, H, m = hb.SottoV38(model=None).encode_low(x)
    E, v, f, uu = hb.frame_map(m, bits, len(H)); return os.path.basename(c), E, v, f, uu, H
if __name__ == '__main__':
    cache = '/tmp/claude-0/hiss/v38_feats.pkl'
    if os.path.exists(cache): TR, TE = pickle.load(open(cache, 'rb'))
    else:
        spk = sorted(s for s in os.listdir(D) if s not in th.TEST)
        with Pool(4) as p:
            TR = p.map(th.one, [(D, s, 20) for s in spk]); TE = p.map(te_one, sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')))
        pickle.dump((TR, TE), open(cache, 'wb'))
    z = np.load(hb.MODEL, allow_pickle=True); W, cb7 = z['W'].item(), z['cb']
    res = np.concatenate([np.array(list(hb.uu_residuals(H, hb.guess(W, E, v), f, uu).values())) for E, v, f, uu, H in TR])
    cb6 = KMeans(64, n_init=4, random_state=0).fit(res).cluster_centers_
    rms = lambda e: np.sqrt(np.mean(np.concatenate(e) ** 2))
    for grp in ('ears', 'libri'):
        G = [t for t in TE if t[0].startswith(grp)]
        e = {k: [] for k in ['uu7', 'uu6', 'V_guess', 'V_slow', 'V_oracle', 'M_guess', 'M_slow', 'M_oracle']}
        for name, E, v, f, uu, H in G:
            p = hb.guess(W, E, v); live = H.max(1) > np.percentile(H.max(1), 40)
            for g, r in hb.uu_residuals(H, p, f, uu).items():
                s = (f == g) & uu & live
                for k, cb in (('uu7', cb7), ('uu6', cb6)):
                    e[k].append(H[s] - p[s] - cb[np.argmin(((cb - r) ** 2).sum(1))])
            for c, cl in ((1, 'V'), (2, 'M')):
                s = (v == c) & live
                e[cl + '_guess'].append(H[s] - p[s])
                e[cl + '_oracle'].append(H[s] - p[s] - (H[s] - p[s]).mean(0))
                # slow: offset refreshed every 1 s from the running mean so far (2 dB steps)
                off = np.zeros(4); acc = np.zeros(4); n = 0; out = []
                for t in range(len(p)):
                    if s[t]:
                        out.append(H[t] - p[t] - off); acc += H[t] - p[t]; n += 1
                    if t % 100 == 99 and n: off = np.round(acc / n / 2) * 2
                e[cl + '_slow'].append(np.array(out).reshape(-1, 4))
        print(grp, ' '.join(f'{k} {rms(vv):.1f}' for k, vv in e.items()))

def qoff(r):
    """4 band offsets -> (level 4 bits, 2 dB steps from -10; tilt 2 bits, -3/-1/1/3 dB per band)"""
    b = np.arange(4) - 1.5
    lvl = r.mean(); tilt = np.polyfit(b, r, 1)[0]
    li = int(np.clip(np.round((lvl + 10) / 2), 0, 15)); ti = int(np.clip(np.round((tilt + 3) / 2), 0, 3))
    return li, ti, (-10 + 2 * li) + (-3 + 2 * ti) * b

if __name__ == '__main__':
    for grp in ('ears', 'libri'):
        G = [t for t in TE if t[0].startswith(grp)]; e = {'V': [], 'M': []}; upd = []
        for name, E, v, f, uu, H in G:
            p = hb.guess(W, E, v); live = H.max(1) > np.percentile(H.max(1), 40)
            off = {1: np.zeros(4), 2: np.zeros(4)}; acc = {1: [], 2: []}; nupd = 0
            nwin = (f.max() // 100) + 1
            for w in range(nwin):
                ts = np.where((f >= 100 * w) & (f < 100 * (w + 1)))[0]
                for t in ts:
                    for c, cl in ((1, 'V'), (2, 'M')):
                        if v[t] == c and live[t]:
                            e[cl].append(H[t] - p[t] - off[c]); acc[c].append(H[t] - p[t])
                nuu = len(np.unique(f[ts][uu[ts]]))
                if nuu >= 16:                                # message got through: update at window end
                    for c in (1, 2):
                        if acc[c]: off[c] = qoff(np.mean(acc[c], 0))[2]
                    nupd += 1
            upd.append(nupd / nwin)
        print(grp, 'side channel (16 bits / 2 s window):', ' '.join(f'{k} {np.sqrt(np.mean(np.array(vv) ** 2)):.1f}' for k, vv in e.items()), f' windows updated {100*np.mean(upd):.0f}%')
