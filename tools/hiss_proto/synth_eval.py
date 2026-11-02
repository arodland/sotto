from bwe import *
from pesq import pesq
from pystoi import stoi
tr = [r[1] for r in pickle.load(open('train.pkl', 'rb'))]
te = pickle.load(open('test.pkl', 'rb'))
bwe = BWE().fit(tr); cb = np.load('cb7.npy')
os.makedirs('out', exist_ok=True)
def variants(d):
    p = bwe.predict(d); p7 = apply_vq(d, p, cb)
    U = (d['v'] == 0).astype(float); UU = d['uu'].astype(float)
    return {
        'nb': None,
        'b0_u': (p, U),
        'b7_uu': (p7, UU),
        'b7_uu_pred_vm-6': (np.where(d['uu'][:, None], p7, p - 6), np.ones(len(U))),
        'oracle_u': (d['hb'], U),
        'oracle_all': (d['hb'], np.ones(len(U))),
    }
R = {}
for d in te:
    for k, v in variants(d).items():
        y = d['y16'] if v is None else d['y16'] + synth_hb(d, v[0], v[1])
        sf.write(f"out/{d['name']}__{k}.wav", y, 16000, subtype='PCM_16')
        R.setdefault(k, []).append(pesq(16000, d['x16'], y, 'wb'))
names = [d['name'] for d in te]
print('variant           ears   libri  all')
for k, v in R.items():
    v = np.array(v); e = np.array([n.startswith('ears') for n in names])
    print(f'{k:18s} {v[e].mean():.3f}  {v[~e].mean():.3f}  {v.mean():.3f}')
