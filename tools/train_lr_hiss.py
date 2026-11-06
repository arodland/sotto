"""Train sotto 1200 hiss's B-frame tables (sim/data/lr_hiss_b.npz).

    python tools/train_lr_hiss.py LIBRISPEECH_DIR [--seconds 30] [--out sim/data/lr_hiss_b.npz]

LIBRISPEECH_DIR is the extracted LibriSpeech tree (dev-clean, CC BY 4.0). The 8 dev-clean
speakers held out as test voices (/mnt/project-files/vocoder/samples-clean/) are skipped.
For every unvoiced B frame the encoder records the 4-band hiss target minus its base (the mean
of the quantised hiss of its unvoiced A neighbours: "rel"; or nothing: "abs"); each table is
k-means over those targets: rel3/abs3 (8 x 4 dB, B_BITS=3) and rel2/abs2 (4 x 4, B_SIDE=True).
"""
import argparse, glob, os, sys
from multiprocessing import Pool
import numpy as np, soundfile as sf
from scipy.signal import resample_poly

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
HELD_OUT = {"2428", "251", "3081", "3536", "6313", "652", "777", "8842"}


def speaker_audio(d, spk, seconds):
    parts, n = [], 0
    for f in sorted(glob.glob(os.path.join(d, "dev-clean", spk, "*", "*.flac"))):
        x, fs = sf.read(f)
        x = resample_poly(x, 16000, fs) if fs != 16000 else x
        parts.append(x)
        n += len(x)
        if n > seconds * 16000:
            break
    x = np.concatenate(parts)[:int(seconds * 16000)]
    x = x[:len(x) // 640 * 640]
    return 0.5 * x / np.abs(x).max()


def targets(args):
    d, spk, seconds = args
    import sotto_lr_hiss as lh
    x = speaker_audio(d, spk, seconds)
    out = {}
    for rel in (True, False):
        c = lh.Sotto1200Hiss(b_model=None, b_rel=rel)
        c.encode(x)
        out["rel" if rel else "abs"] = np.array(c._b_targets).reshape(-1, 4)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", default=os.path.join(ROOT, "sim", "data", "lr_hiss_b.npz"))
    a = ap.parse_args()
    spk = sorted(s for s in os.listdir(os.path.join(a.dir, "dev-clean")) if s not in HELD_OUT)
    with Pool(os.cpu_count()) as p:
        res = p.map(targets, [(a.dir, s, a.seconds) for s in spk])
    from sklearn.cluster import KMeans
    tabs = {}
    for kind in ("rel", "abs"):
        T = np.concatenate([r[kind] for r in res])
        for nb in (2, 3):
            km = KMeans(2 ** nb, n_init=10, random_state=0).fit(T)
            err = np.sqrt(((T - km.cluster_centers_[km.labels_]) ** 2).mean())
            c = km.cluster_centers_[np.argsort(km.cluster_centers_.mean(1))]
            tabs[f"{kind}{nb}"] = np.round(c, 2).astype(np.float32)
            print(f"{kind}{nb}: {len(T)} B frames from {len(spk)} speakers, rms {np.sqrt((T ** 2).mean()):.2f} -> {err:.2f} dB")
    np.savez(a.out, **tabs)
    print("saved", a.out)


if __name__ == "__main__":
    main()
