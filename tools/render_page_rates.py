"""Bitstreams and scores for the page's rate comparison (docs/index.html, "Four rates").

One 16 kHz original per voice (docs/samples/v38/<voice>__original16.flac) is encoded at the
four sotto rates with the native C tools, and the page decodes the frames in the browser
(docs/sotto.wasm, docs/sotto_vlr.wasm), as it does for v3.8 elsewhere:

  2400  sotto v3.8           sotto_enc -16          sotto_dec -16              16 kHz
  1200  sotto 1200 hiss      sotto_enc -16 -1200    sotto_dec -16 -1200        16 kHz
   600  sotto 600            sotto_enc -600         sotto_vlr_dec_fx 600       8 kHz
   400  sotto 400            sotto_enc -400         sotto_vlr_dec_fx 400       8 kHz

Every rate is 48 bits per frame; the frame is 20, 40, 80 or 120 ms long. The 600 / 400
encoders take 8 kHz: the original through scipy's resample_poly(x, 1, 2), rounded (the page
does the same in C, sotto_wasm_down2). Their decoder is the fixed-point one on the v3.7 core
(the code the K6 runs), from a VARIANT=v3.7 build (`make -C c/wasm check` builds one in
build/c37). The input is cut to whole 40 ms blocks first.

PESQ is narrowband (8 kHz), like the page's other scores: it hears only 0-4 kHz, so it misses
the 4-8 kHz hiss band 2400 and 1200 carry. Writes docs/samples/rates/<voice>__<rate>.bit and
docs/results/page_rates.json.

Usage: python tools/render_page_rates.py [--vlr-dec build/c37/fixed/sotto_vlr_dec_fx]
Build first: make (c/sotto_enc, c/sotto_dec, VARIANT=v3.8) and the v3.7 fixed VLR decoder.
"""
import argparse, json, os, subprocess, tempfile
import numpy as np, soundfile as sf
from pesq import pesq
from scipy.signal import resample_poly

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICES = ["female_libri198_14s", "male_libri3436_15s", "librispeech_3536_f", "librispeech_8842_f",
          "librispeech_777_m", "librispeech_251_m"]
RATES = [2400, 1200, 600, 400]


def down2(x16):
    return np.clip(np.rint(resample_poly(x16.astype(float), 1, 2)), -32768, 32767).astype("<i2")


def run(rate, x16, d, vlr_dec):
    """Encode x16 (int16, 16 kHz) at rate; returns (frames bytes, decoded float, its rate)."""
    enc, dec = f"{ROOT}/c/sotto_enc", f"{ROOT}/c/sotto_dec"
    if rate in (2400, 1200):
        x16.astype("<i2").tofile(f"{d}/in.raw")
        flags = ["-16"] + (["-1200"] if rate == 1200 else [])
        subprocess.run([enc, *flags, f"{d}/in.raw", f"{d}/out.bit"], check=True)
        subprocess.run([dec, *flags, f"{d}/out.bit", f"{d}/out.raw"], check=True)
        fs = 16000
    else:
        down2(x16).tofile(f"{d}/in.raw")
        subprocess.run([enc, f"-{rate}", f"{d}/in.raw", f"{d}/out.bit"], check=True)
        subprocess.run([vlr_dec, str(rate), f"{d}/out.bit", f"{d}/out.raw"], check=True)
        fs = 8000
    bits = open(f"{d}/out.bit", "rb").read()
    return bits, np.fromfile(f"{d}/out.raw", "<i2").astype(float) / 32768, fs


def main(vlr_dec, out):
    os.makedirs(out, exist_ok=True)
    res = {}
    for v in VOICES:
        x16, fs = sf.read(f"{ROOT}/docs/samples/v38/{v}__original16.flac", dtype="int16")
        assert fs == 16000
        x16 = x16[:len(x16) // 640 * 640]
        ref8 = resample_poly(x16 / 32768, 1, 2)
        res[v] = {"seconds": round(len(x16) / 16000, 2)}
        for r in RATES:
            with tempfile.TemporaryDirectory() as d:
                bits, y, fy = run(r, x16, d, vlr_dec)
            open(f"{out}/{v}__{r}.bit", "wb").write(bits)
            y8 = resample_poly(y, 1, 2) if fy == 16000 else y
            n = min(len(ref8), len(y8))
            p = float(pesq(8000, ref8[:n], y8[:n], "nb"))
            res[v][str(r)] = {"pesq_nb": round(p, 3), "bytes": len(bits)}
            print(v, r, len(bits), "bytes", f"PESQ-NB {p:.2f}", flush=True)
    res["mean"] = {str(r): round(float(np.mean([res[v][str(r)]["pesq_nb"] for v in VOICES])), 3) for r in RATES}
    print("mean", res["mean"])
    with open(f"{ROOT}/docs/results/page_rates.json", "w") as f:
        json.dump(res, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vlr-dec", default=f"{ROOT}/build/c37/fixed/sotto_vlr_dec_fx")
    ap.add_argument("--out", default=f"{ROOT}/docs/samples/rates")
    a = ap.parse_args()
    main(a.vlr_dec, a.out)
