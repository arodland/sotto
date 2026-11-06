"""Train sotto 600 / 400 hiss's tables (sim/data/vlr_hiss.npz).

    python tools/train_vlr_hiss.py LIBRISPEECH_DIR [--seconds 30] [--out sim/data/vlr_hiss.npz]

LIBRISPEECH_DIR is the extracted LibriSpeech tree (dev-clean, CC BY 4.0); the 8 test speakers
are skipped (tools/train_lr_hiss.py's HELD_OUT). Per mode, the encoder (with the default
tables) records, for superframes with no pitched frame, the slope (dB per frame) of the hiss
level around the chosen 6-bit shape, and for superframes with one pitched frame, the 4-band
hiss target minus the neighbours' mean, and for frames with no bits the hiss target minus the
neighbours' mean (m<mode>_k2) or, with no coded neighbour, the target itself (m<mode>_h0, the
base the decoder then uses). m<mode>_slope: 8 levels (1-D k-means), sorted;
m<mode>_off: 4 x 4 dB (k-means), sorted by mean.
"""
import argparse, os, sys
from multiprocessing import Pool
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from train_lr_hiss import HELD_OUT, speaker_audio          # noqa: E402


def targets(args):
    d, spk, seconds, mode = args
    import sotto_vlr_hiss as vh
    c = vh.SottoVLRHiss(mode, model=None)
    c.encode(speaker_audio(d, spk, seconds))
    return c._targets["slope"], c._targets["off_raw"], c._targets["k2"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", default=os.path.join(ROOT, "sim", "data", "vlr_hiss.npz"))
    a = ap.parse_args()
    from sklearn.cluster import KMeans
    spk = sorted(s for s in os.listdir(os.path.join(a.dir, "dev-clean")) if s not in HELD_OUT)
    tabs = {}
    with Pool(os.cpu_count()) as p:
        for mode in ("600", "400"):
            res = p.map(targets, [(a.dir, s, a.seconds, mode) for s in spk])
            S = np.array([v for r in res for v in r[0]])
            K2 = [t for r in res for t in r[2]]
            h0 = np.mean([d for d, ok in K2 if not ok], 0)
            k2 = np.mean([d for d, ok in K2 if ok], 0)
            tabs[f"m{mode}_h0"], tabs[f"m{mode}_k2"] = np.round(h0, 2).astype(np.float32), np.round(k2, 2).astype(np.float32)
            # one-pitched-frame offsets over the base the decoder will use (h0 with no neighbour)
            O = np.array([np.mean([d if ok else d - h0 for d, ok in sf_], 0) for r in res for sf_ in r[1]])
            print(f"{mode}: h0 {tabs[f'm{mode}_h0']} ({sum(not ok for _, ok in K2)} frames), "
                  f"k2 {tabs[f'm{mode}_k2']} ({sum(ok for _, ok in K2)})", flush=True)
            km = KMeans(8, n_init=10, random_state=0).fit(S[:, None])
            tabs[f"m{mode}_slope"] = np.round(np.sort(km.cluster_centers_[:, 0]), 2).astype(np.float32)
            es = np.sqrt(((S - km.cluster_centers_[km.labels_, 0]) ** 2).mean())
            km = KMeans(4, n_init=10, random_state=0).fit(O)
            eo = np.sqrt(((O - km.cluster_centers_[km.labels_]) ** 2).mean())
            tabs[f"m{mode}_off"] = np.round(km.cluster_centers_[np.argsort(km.cluster_centers_.mean(1))], 2).astype(np.float32)
            print(f"{mode}: {len(S)} slopes, rms {np.sqrt((S ** 2).mean()):.2f} -> {es:.2f} dB/frame, "
                  f"levels {tabs[f'm{mode}_slope']}; {len(O)} offset frames, rms {np.sqrt((O ** 2).mean()):.2f} -> {eo:.2f} dB",
                  flush=True)
    np.savez(a.out, **tabs)
    print("saved", a.out)


if __name__ == "__main__":
    main()
