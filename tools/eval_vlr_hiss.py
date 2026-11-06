"""Objective scores for sotto 600 / 400 with the 4-8 kHz hiss band (docs/sotto-vlr.md, "Hiss band").

    python tools/eval_vlr_hiss.py CLIPS16_DIR [--modes 600,400] [--json docs/results/vlr_hiss.json]
        [--wav OUT_DIR] [--targets OUT.npz]

CLIPS16_DIR: the 16 held-out voices at 16 kHz, as for tools/eval_lr_hiss.py (same metrics:
PESQ-NB / ESTOI on 0-4 kHz, PESQ-WB, fricative 4-8 kHz band error). Variants per mode: plain,
hiss, hiss without the one-pitched-frame offsets (slope kept as pitch), hiss without the side
channel, and a plain decoder reading the hiss stream (the K6). --targets saves the encoder's
level slopes and offsets for tools/train_vlr_hiss.py.
"""
import argparse, glob, json, os, sys
from multiprocessing import Pool
import numpy as np, soundfile as sf
from scipy.signal import resample_poly

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import vocoder_lab as vl                                   # noqa: E402
import sotto_vlr as vr                                     # noqa: E402
import sotto_vlr_hiss as vh                                # noqa: E402
from eval_lr_hiss import up, score, XL_LOST                # noqa: E402

OPT = {}


def variants(x16, mode, loss):
    n = len(x16)
    out, tg = {}, {}
    p = vr.SottoVLR(mode)
    b = p.encode(resample_poly(x16, 1, 2))
    g = None if not loss else (np.arange(len(b)) % 8) != XL_LOST
    out["plain"] = up(p.decode_audio(b, g)[vl.ar.CODEC_DELAY:], n)
    for name, kw in (("hiss", {}), ("hiss_no_offset", dict(slope_hiss=False)), ("hiss_no_side", dict(side=False))):
        if loss and name != "hiss":
            continue
        c = vh.SottoVLRHiss(mode, **kw)
        bh = c.encode(x16)
        gh = None if not loss else (np.arange(len(bh)) % 8) != XL_LOST
        out[name] = c.decode_audio16(bh, gh, n16=n)
        if name == "hiss":
            tg = c._targets
            out["plain_reads_hiss"] = up(p.decode_audio(bh, gh)[vl.ar.CODEC_DELAY:], n)
    return out, tg


def run(args):
    path, mode, loss, wav = args
    x = sf.read(path)[0]
    x = x[:len(x) // 640 * 640]
    k = os.path.basename(path)[:-4]
    out, tg = variants(x, mode, loss)
    if wav:
        os.makedirs(wav, exist_ok=True)
        for name, y in out.items():
            sf.write(os.path.join(wav, f"{k}__{mode}_{name}{'_loss' if loss else ''}.wav"), np.clip(y, -1, 1), 16000)
    return k, {name: score(x, y) for name, y in out.items()}, tg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips")
    ap.add_argument("--modes", default="600,400")
    ap.add_argument("--json")
    ap.add_argument("--wav")
    ap.add_argument("--targets")
    ap.add_argument("--clean-only", action="store_true")
    a = ap.parse_args()
    clips = sorted(glob.glob(os.path.join(a.clips, "*.wav")))
    res, tgs = {}, {}
    with Pool(os.cpu_count()) as pool:
        for mode in a.modes.split(","):
            for loss in (False,) if a.clean_only else (False, True):
                r = pool.map(run, [(c, mode, loss, a.wav) for c in clips])
                rows = {k: s for k, s, _ in r}
                if not loss:
                    tgs[f"m{mode}_slope"] = np.array([v for _, _, t in r for v in t["slope"]])
                    tgs[f"m{mode}_off"] = np.array([v for _, _, t in r for v in t["off"]]).reshape(-1, 4)
                cond = f"{mode}_" + ("xl_1_in_8_lost" if loss else "clean")
                names = list(next(iter(rows.values())))
                mean = {nm: {m: float(np.mean([rows[k][nm][m] for k in rows])) for m in rows[next(iter(rows))][nm]}
                        for nm in names}
                res[cond] = dict(mean=mean, clips=rows)
                print(f"\nsotto {cond} ({len(rows)} voices): PESQ-NB  ESTOI  PESQ-WB  hiss rms / bias dB")
                for nm in names:
                    s = mean[nm]
                    print(f"  {nm:18s} {s['pesq_nb']:.3f}  {s['estoi']:.3f}  {s['pesq_wb']:.3f}"
                          f"   {s['hiss_rms_db']:5.2f} / {s['hiss_bias_db']:+.2f}", flush=True)
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1)
    if a.targets:
        np.savez(a.targets, **tgs)


if __name__ == "__main__":
    main()
