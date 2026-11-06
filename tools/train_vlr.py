"""Train the very-low-rate sotto tables (sim/sotto_vlr.py) from tools/vlr_corpus.py output.

Usage: python tools/train_vlr.py CORPUS.npz [--out sim/data/vlr_tables.npz] [--modes 600,400]
       [--max-frames 200000]

Per mode (frames per superframe N):
  vpat   voicing patterns: Lloyd iterations under sotto_vlr.V_COST, frames weighted by energy
  slope  pitch-line slopes in index steps per frame (1-D k-means on voiced superframes)
  eres   energy residuals after the quantized mean (k-means, N dims)
  a<j>s<i>  anchor j's multistage shape codebook, stage i: vocoder_lab.ShapeMSVQ on the
         non-silent frames, then --loop closed-loop passes (refine_shapes) that count the
         interpolated frames' error too
"""
import argparse, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
import vocoder_lab as vl                                       # noqa: E402
import sotto_vlr as vr                                         # noqa: E402


def superframes(d, N):
    """-> dict of arrays (S, N) for code, wi, ei and (S, N, K) shape, speaker by speaker."""
    out = {k: [] for k in ("code", "wi", "ei", "shape")}
    o = 0
    for n in d["lens"]:
        n = int(n)
        m = n // N * N
        for k in out:
            a = d[k][o:o + m]
            out[k].append(a.reshape((m // N, N) + a.shape[1:]))
        o += n
    return {k: np.concatenate(v) for k, v in out.items()}


def train_vpat(code, w, bits, iters=30):
    n = 2 ** bits
    pats, cnt = np.unique(code, axis=0, return_counts=True)
    pat = pats[np.argsort(-cnt)[:n]].copy()
    for _ in range(iters):
        cost = np.stack([(vr.V_COST[code, p[None]] * w).sum(1) for p in pat], 1)
        lab = cost.argmin(1)
        new = pat.copy()
        for c in range(n):
            sel = lab == c
            if not sel.any():
                continue
            for k in range(code.shape[1]):
                tot = [(vr.V_COST[code[sel, k], q] * w[sel, k]).sum() for q in range(4)]
                new[c, k] = int(np.argmin(tot))
        if np.array_equal(new, pat):
            break
        pat = new
    tc = np.stack([(vr.V_COST[code, p[None]] * w).sum(1) for p in pat], 1).min(1).mean()
    print(f"  voicing {bits} bits: mean weighted cost {tc:.3f}", flush=True)
    return pat


def kmeans1d(x, n, iters=50):
    c = np.quantile(x, (np.arange(n) + 0.5) / n)
    for _ in range(iters):
        lab = np.abs(x[:, None] - c[None]).argmin(1)
        c = np.array([x[lab == i].mean() if (lab == i).any() else c[i] for i in range(n)])
    return np.sort(c)


def train_slopes(code, wi, bits, N):
    t = np.arange(N) - (N - 1) / 2
    s = []
    for c, w in zip(code, wi):
        v = c != 0
        if v.sum() >= 2:
            s.append(np.polyfit(t[v], w[v].astype(float), 1)[0])
    s = np.clip(np.array(s), -8, 8)
    tab = kmeans1d(s, 2 ** bits)
    print(f"  slopes: {np.round(tab, 2)}", flush=True)
    return tab


def train_eres(ei, be, bits):
    from sklearn.cluster import KMeans
    lv = (1 << be) - 1
    m = np.round(ei.mean(1) * lv / 31) * 31 / lv
    r = ei - m[:, None]
    km = KMeans(2 ** bits, n_init=2, random_state=0).fit(r)
    d = ((r - km.cluster_centers_[km.labels_]) ** 2).mean()
    print(f"  energy residual {bits} bits: rms {np.sqrt(d):.2f} codes", flush=True)
    return km.cluster_centers_


def sequences(d):
    """Per speaker: (DCT targets (F, K-1), frame weights (F,))."""
    out, o = [], 0
    for n in d["lens"]:
        n = int(n)
        sh = d["shape"][o:o + n].astype(float)
        out.append(((sh @ vl.DCT.T)[:, 1:], vr.frame_weight(d["ei"][o:o + n])))
        o += n
    return out


_V = None


def _assign(args):
    cc, w = args
    N = _V.N
    m = len(cc) // N * N
    if m < 2 * N:
        return None
    return _V.q_shapes(cc[:m], w[:m]), m


def refine_shapes(mode, tabs, seqs, iters=4, lam=1.0):
    """Closed-loop training: alternate the encoder's Viterbi choice over training sequences
    with a least-squares update of every stage codeword, counting each frame's error through
    the interpolation it is decoded with. Slots with the same stage bits share one codebook."""
    import scipy.sparse as sp
    from multiprocessing import Pool
    global _V
    cfg = vr.MODES[mode]
    p = f"m{mode}_"
    N = cfg["N"]
    groups = {}
    for j, (_, bits) in enumerate(cfg["anchors"]):
        groups.setdefault(tuple(bits), []).append(j)
    # column offsets: one block per shared codebook, stage by stage
    col, off = {}, 0
    for bits, js in groups.items():
        for i, b in enumerate(bits):
            for j in js:
                col[(j, i)] = off
            off += 2 ** b
    for it in range(iters):
        _V = vr.SottoVLR(mode, tables=tabs)
        with Pool(4) as pool:
            res = pool.map(_assign, seqs, chunksize=8)
        rows, cols, vals, X, W, err = [], [], [], [], [], 0.0
        r0 = 0
        for (cc, w), r in zip(seqs, res):
            if r is None:
                continue
            sel, m = r
            anc = _V.anchor_frames(m // N)
            for k, (fa, j) in enumerate(anc):
                fb, jb = anc[k + 1] if k + 1 < len(anc) else (m, None)
                for f in range(fa, fb):
                    t = (f - fa) / (fb - fa) if jb is not None else 0.0
                    for i, idx in enumerate(sel[k]):
                        rows.append(r0 + f); cols.append(col[(j, i)] + idx); vals.append(1 - t)
                    if jb is not None and t > 0:
                        for i, idx in enumerate(sel[k + 1]):
                            rows.append(r0 + f); cols.append(col[(jb, i)] + idx); vals.append(t)
            X.append(cc[:m]); W.append(w[:m])
            r0 += m
        G = sp.csr_matrix((vals, (rows, cols)), shape=(r0, off))
        X, W = np.concatenate(X), np.concatenate(W)
        Cold = np.zeros((off, X.shape[1]))
        for bits, js in groups.items():
            for i in range(len(bits)):
                Cold[col[(js[0], i)]:col[(js[0], i)] + 2 ** bits[i]] = tabs[f"{p}a{js[0]}s{i}"]
        err = float((W[:, None] * (G @ Cold - X) ** 2).sum() / W.sum())
        GW = G.T.multiply(W[None, :]).tocsr()
        A = (GW @ G).toarray() + lam * np.eye(off)
        C = np.linalg.solve(A, GW @ X + lam * Cold)
        for bits, js in groups.items():
            for i in range(len(bits)):
                for j in js:
                    tabs[f"{p}a{j}s{i}"] = C[col[(js[0], i)]:col[(js[0], i)] + 2 ** bits[i]].astype(np.float32)
        print(f"  closed loop {it}: weighted error before update {err:.2f} dB^2 per frame", flush=True)
    return tabs


def main(corpus, out, modes, max_frames, loop=0):
    d = dict(np.load(corpus))
    tabs = dict(np.load(out)) if os.path.exists(out) else {}
    rng = np.random.default_rng(0)
    live = d["ei"] >= 4
    shapes = d["shape"][live]
    if len(shapes) > max_frames:
        shapes = shapes[rng.choice(len(shapes), max_frames, replace=False)]
    print(len(d["code"]), "frames;", len(shapes), "shapes for the codebooks", flush=True)
    cbs = {}
    for mode in modes:
        cfg = vr.MODES[mode]
        N = cfg["N"]
        p = f"m{mode}_"
        print(f"mode {mode} (N={N}, {vr.mode_bits(cfg)} bits)", flush=True)
        S = superframes(d, N)
        w = vr.frame_weight(S["ei"])
        tabs[p + "vpat"] = train_vpat(S["code"].astype(int), w, cfg["bv"])
        tabs[p + "slope"] = train_slopes(S["code"], S["wi"], cfg["bs"], N)
        tabs[p + "eres"] = train_eres(S["ei"].astype(float), cfg["be"], cfg["br"])
        for j, (_, bits) in enumerate(cfg["anchors"]):
            bits = tuple(bits)
            if bits not in cbs:
                q = vl.ShapeMSVQ(shapes, bits=bits, m_best=8, iters=25, joint=2, seed=0)
                cbs[bits] = q.cb
                print(f"  shape codebook {bits} trained", flush=True)
            for i, cb in enumerate(cbs[bits]):
                tabs[f"{p}a{j}s{i}"] = cb.astype(np.float32)
        if loop:
            tabs = refine_shapes(mode, tabs, sequences(d), iters=loop)
    np.savez_compressed(out, **tabs)
    print("wrote", out, flush=True)


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("corpus")
    a.add_argument("--out", default=os.path.join(ROOT, "sim", "data", "vlr_tables.npz"))
    a.add_argument("--modes", default="600,400")
    a.add_argument("--max-frames", type=int, default=200000)
    a.add_argument("--loop", type=int, default=4, help="closed-loop shape training passes")
    a = a.parse_args()
    main(a.corpus, a.out, a.modes.split(","), a.max_frames, a.loop)
