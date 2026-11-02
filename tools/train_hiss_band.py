"""Train sotto v3.8's high-band guess and 7-bit codebook (sim/data/hiss_band.npz).

Usage: python tools/train_hiss_band.py LIBRISPEECH_DEV_CLEAN_DIR [--seconds 20]
Uses 20 s from each dev-clean speaker except the 8 held out as test voices (CC BY 4.0).
The guess: per voicing class, least squares from the decoded 0-4 kHz envelope to the four
4-8 kHz band levels. The codebooks: k-means (128, and 64 for the side-channel mode) of
the per-UU-frame residuals.
"""
import argparse, glob, os, sys
from multiprocessing import Pool
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sim"))
import vocoder_lab as vl
import hiss_band as hb

TEST = {"2428", "251", "3081", "3536", "6313", "652", "777", "8842"}


def one(args):
    d, s, seconds = args
    vl.TRACK2["lag"] = 8
    rng = np.random.default_rng(int(s))
    parts, n = [], 0
    for f in rng.permutation(sorted(glob.glob(f"{d}/{s}/*/*.flac"))):
        x, _ = sf.read(f)
        parts.append(x)
        n += len(x)
        if n > seconds * 16000:
            break
    x = np.concatenate(parts)[:int(seconds * 16000)]
    x = 0.5 * x / np.abs(resample_poly(x, 1, 2)).max()
    bits, H, m = hb.SottoV38(model=None).encode_low(x)
    E, v, f, uu = hb.frame_map(m, bits, len(H))
    return E, v, f, uu, H


def main(d, seconds, out):
    spk = sorted(s for s in os.listdir(d) if s not in TEST and os.path.isdir(os.path.join(d, s)))
    with Pool(4) as p:
        R = p.map(one, [(d, s, seconds) for s in spk])
    W = {}
    for c in range(3):
        A, Y = [], []
        for E, v, f, uu, H in R:
            ref = E[:, 6]
            F = np.column_stack([np.ones(len(E)), E[:, :6] - ref[:, None], ref])
            A.append(F[v == c]); Y.append(H[v == c] - ref[v == c, None])
        a, y = np.concatenate(A), np.concatenate(Y)
        W[c] = np.linalg.solve(a.T @ a + np.eye(a.shape[1]), a.T @ y)
    res = np.concatenate([np.array(list(hb.uu_residuals(H, hb.guess(W, E, v), f, uu).values()))
                          for E, v, f, uu, H in R])
    from sklearn.cluster import KMeans
    cb = KMeans(128, n_init=4, random_state=0).fit(res).cluster_centers_
    cb6 = KMeans(64, n_init=4, random_state=0).fit(res).cluster_centers_   # with the side channel
    np.savez(out, W=np.array(W, dtype=object), cb=cb, cb6=cb6)
    print(f"{len(spk)} speakers, {len(res)} UU frames -> {out}")


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("dir")
    a.add_argument("--seconds", type=float, default=20)
    a.add_argument("--out", default=hb.MODEL)
    a = a.parse_args()
    main(a.dir, a.seconds, a.out)
