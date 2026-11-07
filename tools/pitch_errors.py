"""Where sotto's pitch goes wrong: each clip's pitch, frame by frame, against librosa's pYIN.

Usage: python tools/pitch_errors.py [--samples DIR] [--modes track,1200,600,400] [--json OUT]
       [--spots N] [--clips NAME,...]

Per 10 ms frame, as each decoder plays it (its synthesis model's pitch and voicing), it compares:
  track   the encoder's pitch tracker (v3.7, vocoder_lab.analyse "tracked4"), before any
          quantizing: the pitch every mode starts from
  2400    sotto v3.7 (2400 bit/s), decoded
  1200    sotto 1200's decoded pitch (7-bit anchors, 3-bit steps between)
  600/400 the very-low-rate modes' decoded pitch (a straight line through each 80/120 ms
          superframe's voiced frames)
against pYIN (fmin 50, fmax 500 Hz) on the same 8 kHz audio, where pYIN is fairly sure
(where its HMM calls the frame voiced) and the level is above -40 dBFS, as docs/encoder-v1.md's v3.7
count did. Counts:
  gross   voiced in both, more than 1.6 times off (an octave-type error)
  off20   voiced in both, more than 20% off (the usual gross pitch error, GPE)
  cents   RMS error in cents over voiced frames that are within 20% (the fine contour)
  miss    pYIN voiced, sotto unvoiced, above -40 dBFS
  false   pYIN unvoiced, sotto voiced, above -40 dBFS
and lists the worst spots as time indexes (s), a run of bad frames each, with what sotto
played and what pYIN heard. pYIN isn't ground truth (it clamps some high voices to its floor),
so a spot is a place to listen, not a verdict.
SPDX-License-Identifier: MIT
"""
import argparse, glob, json, os, sys
from multiprocessing import Pool
import numpy as np, soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
import vocoder_lab as vl                                       # noqa: E402
import sotto_lr as lr                                          # noqa: E402
import sotto_vlr as vr                                         # noqa: E402

FS = 8000
HOP = 160                       # 20 ms
SAMPLES = "/mnt/project-files/vocoder/samples-clean"


def frame_time(k):
    """centre of 10 ms frame k in seconds: its 320-sample buffer (after c2sim's 240-sample
    lead) is centred on sample 80 k - 80"""
    return (80 * k - 80) / FS


def reference(x, nf):
    """pYIN f0 (Hz, nan where its HMM says unvoiced), a sureness flag (1 voiced, 0 unvoiced)
    and the level (dBFS) at each 10 ms frame"""
    import librosa
    f0, flag, _ = librosa.pyin(x, fmin=50, fmax=500, sr=FS, frame_length=512, hop_length=80,
                               center=True)
    idx = np.clip(np.arange(nf) - 1, 0, len(f0) - 1)    # pYIN frame j is centred on sample 80 j
    lv = np.full(nf, -120.0)
    for k in range(nf):
        c = int(80 * k - 80)
        seg = x[max(0, c - 160):max(0, c + 160)]
        if len(seg):
            lv[k] = 10 * np.log10(np.mean(seg ** 2) + 1e-12)
    return np.where(flag[idx], f0[idx], np.nan), flag[idx].astype(float), lv


CODECS = {"2400": lambda: vl.SottoV37(), "1200": lambda: lr.Sotto1200(),
          "600": lambda: vr.SottoVLR("600"), "400": lambda: vr.SottoVLR("400")}


def tracks(x, modes):
    """per-mode (f0 Hz per 10 ms frame, voiced flags) as the decoder plays them; f0 is nan
    where unvoiced. "track" is the encoder's tracker before any quantizing."""
    out = {}
    m = vl.analyse(x, "tracked4")
    nf = len(m)
    out["track"] = m
    for name in modes:
        if name != "track":
            v = CODECS[name]()
            out[name] = v.decode(v.encode(x))
    res = {}
    for name in modes:
        d = out[name][:nf]
        hz = np.full(nf, np.nan)
        vo = np.zeros(nf, bool)
        vo[:len(d)] = d["voiced"] > 0
        hz[:len(d)] = d["Wo"] * FS / (2 * np.pi)
        res[name] = (np.where(vo, hz, np.nan), vo)
    return res, nf


def compare(ref, prob, lv, f0, vo):
    sure = (lv > -40) & np.isfinite(ref)
    both = sure & vo
    r = np.where(both, f0 / np.where(np.isfinite(ref), ref, 1), 1.0)
    gross = both & ((r > 1.6) | (r < 1 / 1.6))
    off20 = both & ((r > 1.2) | (r < 1 / 1.2))
    fine = both & ~off20
    cents = 1200 * np.log2(r[fine]) if fine.any() else np.zeros(1)
    miss = (lv > -40) & np.isfinite(ref) & ~vo
    false = (lv > -40) & ~np.isfinite(ref) & vo
    return dict(voiced=int(both.sum()), gross=int(gross.sum()), off20=int(off20.sum()),
                cents=round(float(np.sqrt(np.mean(cents ** 2))), 1), miss=int(miss.sum()),
                false=int(false.sum())), off20, miss, false, r


def spots(bad, kind, f0, ref, r, nmax):
    """runs of bad frames: (start s, end s, frames, kind, sotto Hz, pYIN Hz, worst ratio)"""
    out, f = [], 0
    n = len(bad)
    while f < n:
        if not bad[f]:
            f += 1
            continue
        g = f
        while g < n and (bad[g] or (g + 1 < n and bad[g + 1])):    # bridge one-frame gaps
            g += 1
        sl = slice(f, g)
        worst = float(np.nanmax(np.abs(np.log2(r[sl])))) if kind == "pitch" else 0.0
        out.append(dict(t0=round(frame_time(f), 2), t1=round(frame_time(g), 2), frames=int(bad[sl].sum()),
                        kind=kind, sotto_hz=round(float(np.nanmedian(f0[sl])), 0) if np.isfinite(f0[sl]).any() else None,
                        ref_hz=round(float(np.nanmedian(ref[sl])), 0) if np.isfinite(ref[sl]).any() else None,
                        octaves=round(worst, 2)))
        f = g
    out.sort(key=lambda s: (-s["frames"] * (1 + s["octaves"]), s["t0"]))
    return out[:nmax]


def job(args):
    path, modes, nspots = args
    x, fs = sf.read(path)
    assert fs == FS
    tr, nf = tracks(x, modes)
    ref, prob, lv = reference(x, nf)
    res = {}
    for m in modes:
        f0, vo = tr[m]
        st, off20, miss, false, r = compare(ref, prob, lv, f0, vo)
        sp = spots(off20, "pitch", f0, ref, r, nspots) + spots(miss, "missed voicing", f0, ref, r, nspots // 2) \
            + spots(false, "false voicing", f0, ref, r, nspots // 2)
        res[m] = dict(stats=st, spots=sp)
    return os.path.splitext(os.path.basename(path))[0], res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", default=SAMPLES)
    ap.add_argument("--modes", default="track,2400,1200,600,400")
    ap.add_argument("--clips")
    ap.add_argument("--spots", type=int, default=6)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    modes = a.modes.split(",")
    clips = sorted(glob.glob(os.path.join(a.samples, "*.flac")))
    if a.clips:
        clips = [c for c in clips if any(k in c for k in a.clips.split(","))]
    with Pool(min(8, os.cpu_count() or 1)) as pool:
        out = dict(pool.map(job, [(c, modes, a.spots) for c in clips]))
    keys = ["voiced", "gross", "off20", "cents", "miss", "false"]
    print(f"{'clip':26s} " + " ".join(f"{m:>28s}" for m in modes))
    print(f"{'':26s} " + " ".join(f"{'gross off20 cents miss false':>28s}" for _ in modes))
    tot = {m: {k: 0 for k in keys} for m in modes}
    for c, res in out.items():
        print(f"{c:26s} " + " ".join(
            f"{res[m]['stats']['gross']:5d} {res[m]['stats']['off20']:5d} {res[m]['stats']['cents']:5.0f} "
            f"{res[m]['stats']['miss']:4d} {res[m]['stats']['false']:5d}" for m in modes))
        for m in modes:
            for k in keys:
                tot[m][k] += res[m]["stats"][k] * (res[m]["stats"]["voiced"] if k == "cents" else 1)
    for m in modes:
        tot[m]["cents"] = round(tot[m]["cents"] / max(tot[m]["voiced"], 1), 1)
    print(f"{'TOTAL':26s} " + " ".join(
        f"{tot[m]['gross']:5d} {tot[m]['off20']:5d} {tot[m]['cents']:5.0f} {tot[m]['miss']:4d} {tot[m]['false']:5d}"
        for m in modes))
    print(f"voiced frames compared: " + ", ".join(f"{m} {tot[m]['voiced']}" for m in modes))
    if a.json:
        json.dump(dict(total=tot, clips=out), open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
