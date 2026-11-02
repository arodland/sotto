from bwe import *
tr = [r[1] for r in pickle.load(open('train.pkl', 'rb'))]
te = pickle.load(open('test.pkl', 'rb'))
def rms(e): return np.sqrt(np.mean(e ** 2))
for lam in [1, 100, 1e4]:
    bwe = BWE().fit(tr, lam); cb = np.load('cb7.npy') if lam == 1 else fit_vq(tr, bwe, 7)
    for grp in ['ears', 'libri']:
        G = [d for d in te if d['name'].startswith(grp)]
        line = []
        for name in ['pred', 'vq7', 'spk']:
            e = {c: [] for c in range(3)}
            for d in G:
                p = bwe.predict(d)
                if name == 'vq7': p = apply_vq(d, p, cb)
                if name == 'spk':
                    for c in range(3):
                        s = d['v'] == c; p[s] += (d['hb'][s] - p[s]).mean(0)
                live = d['hb'].max(1) > np.percentile(d['hb'].max(1), 40)
                for c in range(3):
                    s = (d['v'] == c) & live; e[c].append(d['hb'][s] - p[s])
            line.append(f'{name} ' + '/'.join(f'{rms(np.concatenate(e[c])):.1f}' for c in range(3)))
        print(f'lam {lam:g} {grp:5s} (U/V/M): ' + '   '.join(line))
