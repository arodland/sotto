"""Objective scores for sotto 1200 with the 4-8 kHz hiss band (docs/sotto-1200-hiss.md).

    python tools/eval_lr_hiss.py CLIPS16_DIR [--json docs/results/lr_hiss.json] [--wav OUT_DIR]

CLIPS16_DIR holds the 16 held-out test voices at 16 kHz (*.wav, same cuts as
/mnt/project-files/vocoder/samples-clean/, made from LibriSpeech dev-clean and EARS).
Per clip and variant: PESQ-NB and ESTOI on the 0-4 kHz part, PESQ-WB at 16 kHz, and the 4-8 kHz
band error on fricative frames (input 4-8 kHz within 10 dB of its 0-4 kHz, in the loudest 70%
of frames): rms over the four 1 kHz bands of output dB minus input dB, and its mean (bias).
"""
import argparse, glob, json, os, sys
from multiprocessing import Pool
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
from pesq import pesq
from pystoi import stoi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
import vocoder_lab as vl                                   # noqa: E402
import hiss_band as hb                                     # noqa: E402
import sotto_lr as lr                                      # noqa: E402
import sotto_lr_hiss as lh                                 # noqa: E402

XL_LOST = 3                   # 1 XL packet in 8 lost: every superframe with index % 8 == 3


def up(y8, n16):
    y8 = np.pad(y8, (0, max(0, n16 // 2 - len(y8))))[:n16 // 2]
    return resample_poly(y8, 2, 1)


def variants(x16, loss):
    """-> {name: 16 kHz output aligned with x16}"""
    n = len(x16)
    x8 = resample_poly(x16, 1, 2)
    out = {}
    p = lr.Sotto1200()
    b = p.encode(x8)
    g = None if not loss else (np.arange(len(b)) % 8) != XL_LOST
    out["lr"] = up(p.decode_audio(b, g)[vl.ar.CODEC_DELAY:], n)
    for name, kw in (("lr_hiss", {}), ("lr_hiss_side", dict(b_side=True)),
                     ("lr_hiss_abs", dict(b_rel=False)), ("lr_hiss_anchor", dict(b_model=None))):
        c = lh.Sotto1200Hiss(**kw)
        bh = c.encode(x16)
        out[name] = c.decode_audio16(bh, g, n16=n)
        if name == "lr_hiss":
            # what the K6 hears: a plain sotto 1200 decoder (8 kHz) reading the hiss stream
            out["k6_reads_hiss"] = up(p.decode_audio(bh, g)[vl.ar.CODEC_DELAY:], n)
            out["lr_hiss_8k"] = up(resample_poly(out[name], 1, 2), n)
    if not loss:
        v = hb.SottoV38(**lh.V38)
        out["v38"] = v.decode_audio16(v.encode(x16), n16=n)
    return out


def hiss_err(x16, y16):
    n = min(len(x16), len(y16))
    PX, PY = (np.abs(hb.stft(s[:n])) ** 2 for s in (x16, y16))
    hx, hy = hb.bands_db(PX, hb.HB), hb.bands_db(PY, hb.HB)
    top = hb.bands_db(PX, [(4000, 8000)])[:, 0]
    low = hb.bands_db(PX, [(0, 4000)])[:, 0]
    tot = 10 * np.log10(PX.sum(1) + 1e-12)
    fr = (top > low - 10) & (tot > np.percentile(tot, 30))
    d = (hy - hx)[fr]
    return float(np.sqrt((d ** 2).mean())), float(d.mean()), int(fr.sum())


def score(x16, y16):
    n = min(len(x16), len(y16))
    x16, y16 = x16[:n], y16[:n]
    x8, y8 = resample_poly(x16, 1, 2), resample_poly(y16, 1, 2)
    e = hiss_err(x16, y16)
    return dict(pesq_nb=float(pesq(8000, x8, y8, "nb")), estoi=float(stoi(x8, y8, 8000, extended=True)),
                pesq_wb=float(pesq(16000, x16, y16, "wb")), hiss_rms_db=e[0], hiss_bias_db=e[1], hiss_frames=e[2])


def run(args):
    path, loss, wav = args
    x = sf.read(path)[0]
    x = x[:len(x) // 640 * 640]
    k = os.path.basename(path)[:-4]
    out = variants(x, loss)
    if wav:
        os.makedirs(wav, exist_ok=True)
        for name, y in out.items():
            sf.write(os.path.join(wav, f"{k}__{name}{'_loss' if loss else ''}.wav"), np.clip(y, -1, 1), 16000)
    return k, {name: score(x, y) for name, y in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips")
    ap.add_argument("--json")
    ap.add_argument("--wav")
    a = ap.parse_args()
    clips = sorted(glob.glob(os.path.join(a.clips, "*.wav")))
    res = {}
    with Pool(os.cpu_count()) as p:
        for loss in (False, True):
            rows = dict(p.map(run, [(c, loss, a.wav) for c in clips]))
            cond = "xl_1_in_8_lost" if loss else "clean"
            names = list(next(iter(rows.values())))
            mean = {n: {m: float(np.mean([rows[k][n][m] for k in rows])) for m in rows[next(iter(rows))][n]} for n in names}
            res[cond] = dict(mean=mean, clips=rows)
            print(f"\n{cond} ({len(rows)} voices): PESQ-NB  ESTOI  PESQ-WB  hiss rms / bias dB")
            for n in names:
                s = mean[n]
                print(f"  {n:16s} {s['pesq_nb']:.3f}  {s['estoi']:.3f}  {s['pesq_wb']:.3f}   {s['hiss_rms_db']:5.2f} / {s['hiss_bias_db']:+.2f}", flush=True)
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
