from bwe import *
tr = [r[1] for r in pickle.load(open('train.pkl', 'rb'))]; te = pickle.load(open('test.pkl', 'rb'))
bwe = BWE().fit(tr); cb = np.load('cb7.npy')
def rms(e): return np.sqrt(np.mean(np.concatenate(e) ** 2))
res = {k: {c: [] for c in range(3)} for k in ['pred', 'carry', 'carry_decay', 'const_off']}
for d in te:
    p = bwe.predict(d); fr = frame_resid(d, p)
    q = {f: cb[np.argmin(((cb - r) ** 2).sum(1))] for f, r in fr.items()}
    live = d['hb'].max(1) > np.percentile(d['hb'].max(1), 40)
    last, age = np.zeros(4), 999; pc, pd = p.copy(), p.copy()
    for t in range(len(p)):
        f = d['frame'][t]
        if d['uu'][t] and f in q: last, age = q[f], 0
        else: age += 1
        if not d['uu'][t]:
            pc[t] += last; pd[t] += last * 0.5 ** (age / 50)
    for c in range(3):
        s = (d['v'] == c) & live & ~d['uu']
        res['pred'][c].append(d['hb'][s] - p[s]); res['carry'][c].append(d['hb'][s] - pc[s]); res['carry_decay'][c].append(d['hb'][s] - pd[s])
        res['const_off'][c].append(d['hb'][s] - p[s] - 0)
for k in ['pred', 'carry', 'carry_decay']:
    print(f'{k:12s} non-UU frames error U/V/M: ' + ' / '.join(f'{rms(res[k][c]):.1f}' for c in range(3)))
