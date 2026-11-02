from common import *
import pickle
from sklearn.cluster import KMeans

def fx(E):
    ref = E[:, 6]
    return np.column_stack([np.ones(len(E)), E[:, :6] - ref[:, None], ref]), ref

class BWE:
    def fit(self, data, lam=1.0):
        self.W = {}
        A = {c: [] for c in range(3)}; Y = {c: [] for c in range(3)}
        for d in data:
            F, ref = fx(d['E'])
            for c in range(3):
                s = d['v'] == c
                A[c].append(F[s]); Y[c].append(d['hb'][s] - ref[s, None])
        for c in range(3):
            a, y = np.concatenate(A[c]), np.concatenate(Y[c])
            self.W[c] = np.linalg.solve(a.T @ a + lam * np.eye(a.shape[1]), a.T @ y)
        return self
    def predict(self, d):
        F, ref = fx(d['E'])
        out = np.zeros((len(F), 4))
        for c in range(3):
            s = d['v'] == c
            out[s] = F[s] @ self.W[c] + ref[s, None]
        return out

def frame_resid(d, pred):
    """mean residual (hb - pred) over the STFT frames of each UU codec frame -> {frame: vec}"""
    r = d['hb'] - pred; out = {}
    for f in np.unique(d['frame'][d['uu']]):
        s = (d['frame'] == f) & d['uu']
        out[f] = r[s].mean(0)
    return out

def fit_vq(data, bwe, bits=7):
    R = np.concatenate([np.array(list(frame_resid(d, bwe.predict(d)).values())) for d in data])
    return KMeans(2 ** bits, n_init=4, random_state=0).fit(R).cluster_centers_

def apply_vq(d, pred, cb):
    p = pred.copy()
    for f, r in frame_resid(d, pred).items():
        s = (d['frame'] == f) & d['uu']
        p[s] += cb[np.argmin(((cb - r) ** 2).sum(1))]
    return p

def synth_hb(d, hbdb, mask=None, seed=0):
    """random-phase noise with band levels hbdb (dB per bin, STFT domain) above 3.9 kHz."""
    rng = np.random.default_rng(seed)
    n = len(hbdb)
    lvl = np.stack([np.interp(FR, HBC, h) for h in hbdb])          # dB per bin
    g = np.clip((FR - 3900) / 200, 0, 1)                            # fade in 3.9-4.1 kHz
    mag = np.sqrt(10 ** (lvl / 10)) * g
    if mask is not None:
        mag *= mask[:, None]
    ph = np.exp(2j * np.pi * rng.random(mag.shape))
    return istft(mag * ph, len(d['y16']))
