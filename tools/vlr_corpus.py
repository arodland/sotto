"""Training sequences for the very-low-rate sotto modes (sim/sotto_vlr.py) from Google Speech
Commands v0.02 (CC BY 4.0, Warden 2018). Download as in tools/sc_corpus.py.

Usage: python tools/vlr_corpus.py SC_DIR OUT.npz [--speakers 800] [--exclude-clips DIR]

Unlike tools/sc_corpus.py, which keeps only shapes, this keeps each speaker's whole frame
sequence (20 ms frames, as sotto 1200's analysis gives them): voicing code, 7-bit pitch index,
5-bit energy code and the mean-removed rate-K shape. The low-rate modes code several frames
jointly, so their tables are trained on runs of frames. Speakers are taken half above and
half below 165 Hz. Speakers of the clips in --exclude-clips (file names *_<speaker>.flac,
as in /mnt/project-files/vocoder/samples-sc/) are left out.
"""
import argparse, glob, os, sys
from multiprocessing import Pool
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "sim"))
sys.path.insert(0, HERE)
import sotto_lr as lr                                          # noqa: E402
from sc_corpus import speaker_clip                             # noqa: E402



def analyse(x):
    if x is None:
        return None
    x = x / (np.sqrt(np.mean(x ** 2)) + 1e-9) * 10 ** (-26 / 20) * 1.6
    x = np.clip(x, -0.99, 0.99)
    p = lr.Sotto1200().params(x)
    v = p["code"] != 0
    f0 = float(np.median(p["wo"][v]) * 8000 / (2 * np.pi)) if v.sum() > 10 else 0.0
    return f0, p["code"].astype(np.int8), p["wi"].astype(np.int8), p["ei"].astype(np.int8), \
        p["shape"].astype(np.float32)


def main(d, out, n_spk, exclude_dir, max_s):
    ex = set()
    if exclude_dir:
        ex = {os.path.splitext(f)[0].split("_")[-1] for f in os.listdir(exclude_dir) if f.endswith(".flac")}
    by = {}
    for w in sorted(os.listdir(d)):
        if not os.path.isdir(os.path.join(d, w)) or w.startswith("_"):
            continue
        for f in os.listdir(os.path.join(d, w)):
            s = f.split("_")[0]
            if s not in ex:
                by.setdefault(s, []).append(f"{w}/{f}")
    spk = sorted((s for s in by if len(by[s]) >= 15), key=lambda s: (-len(by[s]), s))
    print(len(spk), "speakers with 15+ words;", len(ex), "excluded", flush=True)
    rng = np.random.default_rng(1)
    cand = spk[:3 * n_spk]
    with Pool(4) as p:
        clips = p.map(speaker_clip, [(d, sorted(by[s], key=lambda _: rng.random()), max_s) for s in cand])
        res = p.map(analyse, clips)
    rows = [(s, r) for s, r in zip(cand, res) if r is not None and r[0] > 0]
    hi = [r for r in rows if r[1][0] >= 165][:n_spk // 2]
    lo = [r for r in rows if r[1][0] < 165][:n_spk - len(hi)]
    keep = hi + lo
    print(f"{len(rows)} analysed; keeping {len(hi)} higher and {len(lo)} lower voices", flush=True)
    cat = lambda i: np.concatenate([r[1][i] for r in keep])
    lens = np.array([len(r[1][1]) for r in keep])
    np.savez_compressed(out, code=cat(1), wi=cat(2), ei=cat(3), shape=cat(4), lens=lens,
                        names=[r[0] for r in keep], f0=[r[1][0] for r in keep])
    print(lens.sum(), "frames saved to", out, flush=True)


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("sc_dir")
    a.add_argument("out")
    a.add_argument("--speakers", type=int, default=800)
    a.add_argument("--exclude-clips")
    a.add_argument("--max-seconds", type=float, default=8.0)
    a = a.parse_args()
    main(a.sc_dir, a.out, a.speakers, a.exclude_clips, a.max_seconds)
