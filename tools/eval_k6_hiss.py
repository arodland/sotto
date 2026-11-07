"""The K6 light hiss layer against the full v3.8 layer (docs/k6-hiss-port.md), decoder only.

    python tools/eval_k6_hiss.py CLIPS16_DIR [--modes 600,400,1200] [--json OUT] [--wav OUT_DIR]

Each clip is encoded once by the normal hiss encoder (sotto 600 / 400 / 1200 hiss) and decoded
by the full layer and by the light variants, which differ only in the decoder:
  full        the v3.8 layer as the C port runs it
  light       the vowel predictor reads the decoded model, not an STFT of the decoded audio; the
              hiss made at 8 kHz and folded in through the interpolator; v3.7's unvoiced halves
  insynth     the light layer with no inverse FFT: 4 band gains a frame and four order-3 filters
              at 8 kHz on one noise, no look at later frames' audio (made with the core's output,
              -DFX_HISS_FB)
  pair        + the 6-8 kHz bands reuse the 4-6 kHz filters' output, (-1)^n modulated (half
              the filters)
  hop2        + band gains every 20 ms instead of 10
  plain       the plain 8 kHz decoder reading the same bits (what the K6 plays today)
Scores as tools/eval_lr_hiss.py (PESQ-NB / ESTOI on 0-4 kHz, PESQ-WB, fricative 4-8 kHz band
error), plus vowel_rms_db / vowel_bias_db: the 4-8 kHz band error on vowel frames (0-4 kHz at
least 20 dB over 4-8 kHz, level in the top 70%).
SPDX-License-Identifier: MIT
"""
import argparse, glob, json, os, sys
from multiprocessing import Pool
import numpy as np, soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import vocoder_lab as vl                                   # noqa: E402
import hiss_band as hb                                     # noqa: E402
import sotto_lr as lr                                      # noqa: E402
import sotto_lr_hiss as lh                                 # noqa: E402
import sotto_vlr as vr                                     # noqa: E402
import sotto_vlr_hiss as vh                                # noqa: E402
from eval_lr_hiss import up, score                         # noqa: E402

LIGHT = {"full": {},
         "light": dict(vowel_src="model", synth8=True, uv50=False),
         "insynth": dict(vowel_src="model", uv50=False, fb=4, fb_order=3, causal="none", synth8=True),
         "pair": dict(vowel_src="model", uv50=False, fb=4, fb_order=3, causal="none", synth8=True, fb_pair=True),
         "hop2": dict(vowel_src="model", uv50=False, fb=4, fb_order=3, causal="none", synth8=True, fb_hop2=True)}


def vowel_err(x16, y16):
    n = min(len(x16), len(y16))
    PX, PY = (np.abs(hb.stft(s[:n])) ** 2 for s in (x16, y16))
    hx, hy = hb.bands_db(PX, hb.HB), hb.bands_db(PY, hb.HB)
    top = hb.bands_db(PX, [(4000, 8000)])[:, 0]
    low = hb.bands_db(PX, [(0, 4000)])[:, 0]
    tot = 10 * np.log10(PX.sum(1) + 1e-12)
    fr = (low > top + 20) & (tot > np.percentile(tot, 30))
    d = (hy - hx)[fr]
    return float(np.sqrt((d ** 2).mean())), float(d.mean())


def make(mode, **kw):
    return lh.Sotto1200Hiss(**kw) if mode == "1200" else vh.SottoVLRHiss(mode, **kw)


def run(args):
    path, mode, wav = args
    x = sf.read(path)[0]
    x = x[:len(x) // 640 * 640]
    n = len(x)
    k = os.path.basename(path)[:-4]
    bits = make(mode).encode(x)
    out = {name: make(mode, **kw).decode_audio16(bits, n16=n) for name, kw in LIGHT.items()}
    p = lr.Sotto1200() if mode == "1200" else vr.SottoVLR(mode)
    out["plain"] = up(p.decode_audio(bits)[vl.ar.CODEC_DELAY:], n)
    res = {}
    for name, y in out.items():
        s = score(x, y)
        s["vowel_rms_db"], s["vowel_bias_db"] = vowel_err(x, y)
        res[name] = s
        if wav:
            os.makedirs(wav, exist_ok=True)
            sf.write(os.path.join(wav, f"{k}__{mode}_{name}.wav"), np.clip(y, -1, 1), 16000)
    return k, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips")
    ap.add_argument("--modes", default="600,400,1200")
    ap.add_argument("--json")
    ap.add_argument("--wav")
    a = ap.parse_args()
    clips = sorted(glob.glob(os.path.join(a.clips, "*.wav")))
    res = {}
    with Pool(os.cpu_count()) as pool:
        for mode in a.modes.split(","):
            rows = dict(pool.map(run, [(c, mode, a.wav) for c in clips]))
            names = list(next(iter(rows.values())))
            keys = list(rows[next(iter(rows))][names[0]])
            mean = {nm: {m: float(np.mean([rows[k][nm][m] for k in rows])) for m in keys} for nm in names}
            res[mode] = dict(mean=mean, clips=rows)
            print(f"\nsotto {mode} hiss ({len(rows)} voices): PESQ-NB  ESTOI  PESQ-WB  fricative rms/bias  vowel rms/bias dB")
            for nm in names:
                s = mean[nm]
                print(f"  {nm:10s} {s['pesq_nb']:.3f}  {s['estoi']:.3f}  {s['pesq_wb']:.3f}"
                      f"   {s['hiss_rms_db']:5.2f} / {s['hiss_bias_db']:+.2f}"
                      f"   {s['vowel_rms_db']:5.2f} / {s['vowel_bias_db']:+.2f}", flush=True)
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
