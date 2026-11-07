"""Check the sotto 600 / 400 hiss C port (c/sotto_hiss.c sotto_vlr_enc16 / sotto_vlr_dec16,
VARIANT=v3.8) against the Python reference (sim/sotto_vlr_hiss.py SottoVLRHiss).

Usage: python tools/check_vlr_hiss.py CLIP16K.wav [...] [--modes 600,400] [--lag 10]
       [--loss 0.1 | --every 8] [--seconds 8] [--json OUT.json]

As tools/check_lr_hiss.py. Encoder: both encoders see the same 16 kHz samples; the Python runs
the fixed-lag tracker (TRACK2["lag"] = 8) and judges whisper runs on their first lag + 1
frames, as the C does with SOTTO_RULE_LAG = lag. Reports the fraction of superframes whose 48
bits match and which fields differ (wc and ws carry the hiss in superframes with fewer than
two pitched frames). Decoder: both decoders get the Python's bits (and the same loss pattern,
one flag per superframe); the C tool removes its SOTTO_VLR_DEC16_DELAY. Reports the 0-4 kHz
sample SNR between the two outputs, the 4-8 kHz level difference per 10 ms (mean and 95th
percentile of |dB| over frames above -60 dB; the noise band has random phase), and PESQ-NB
against the original for Python, C decoder on Python bits, and C encoder into C decoder.
Build first: make -C c (VARIANT=v3.8, the default).
SPDX-License-Identifier: MIT
"""
import argparse, json, os, subprocess, sys, tempfile
import numpy as np, soundfile as sf
from scipy.signal import resample_poly

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, HERE)
import vocoder_lab as vl                                       # noqa: E402
import audio_race as ar                                        # noqa: E402
import sotto_vlr_hiss as vh                                    # noqa: E402
from check_c_port38 import snr_db, band_track                  # noqa: E402
from check_lr_hiss import compare_bits                         # noqa: E402


def c_encode(x16, mode):
    with tempfile.TemporaryDirectory() as d:
        x16.astype("<i2").tofile(f"{d}/in.raw")
        subprocess.run([f"{ROOT}/c/sotto_enc", "-16", f"-{mode}", f"{d}/in.raw", f"{d}/out.bit"], check=True)
        b = np.fromfile(f"{d}/out.bit", np.uint8)
    return np.unpackbits(b).reshape(-1, 48).astype(np.int8)


def c_decode(bits, mode, good=None):
    with tempfile.TemporaryDirectory() as d:
        np.packbits(np.asarray(bits, np.uint8).reshape(-1, 48), axis=1).tofile(f"{d}/in.bit")
        args = [f"{ROOT}/c/sotto_dec", "-16", f"-{mode}", f"{d}/in.bit", f"{d}/out.raw"]
        if good is not None:
            np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
            args.append(f"{d}/good.u8")
        subprocess.run(args, check=True)
        return np.fromfile(f"{d}/out.raw", "<i2").astype(float) / 32768


def main(clips, modes, lag, loss, every, seconds, out):
    vl.TRACK2["lag"] = 8
    rows = []
    for mode in modes:
        enc = vh.SottoVLRHiss(mode, whisper_lag=lag)
        dec = vh.SottoVLRHiss(mode)
        for path in clips:
            x, fs = sf.read(path)
            assert fs == 16000
            if x.ndim > 1:
                x = x[:, 0]
            if seconds:
                x = x[:int(seconds * fs)]
            x = x[:len(x) // 320 * 320]
            x16 = np.round(x * 32767).astype(np.int16)
            xq = x16 / 32767.0
            bp = enc.encode(xq)
            bc = c_encode(x16, mode)
            exact, diff = compare_bits(enc, bp, bc)
            good = None
            if every:
                good = np.arange(len(bp)) % every != 3 % every     # as tools/eval_vlr_hiss.py
            elif loss:
                good = np.random.default_rng(1).random(len(bp)) >= loss
                good[0] = True
            yp = dec.decode_audio16(bp, good, n16=len(xq))
            yc = c_decode(bp, mode, good)
            n = min(len(yp), len(yc))
            lo_p, lo_c = resample_poly(yp[:n], 1, 2), resample_poly(yc[:n], 1, 2)
            hp, hc = band_track(yp[:n]), band_track(yc[:n])
            d = np.abs(hp - hc)[hp > -60]
            x8 = resample_poly(xq, 1, 2)
            ycc = c_decode(bc, mode, good)[:n]
            r = {"mode": mode, "clip": os.path.basename(path), "superframes": [len(bp), len(bc)],
                 "enc_bit_exact": exact, "field_mismatch": diff,
                 "dec_len": [len(yp), len(yc)], "dec_low_snr_db": round(float(snr_db(lo_p, lo_c)), 1),
                 "dec_hiss_level_diff_db": [round(float(d.mean()), 2), round(float(np.percentile(d, 95)), 2)] if len(d) else None,
                 "pesq_py": round(float(ar.score(x8, lo_p, ar.CODEC_DELAY)[0]), 3),
                 "pesq_c": round(float(ar.score(x8, lo_c, ar.CODEC_DELAY)[0]), 3),
                 "pesq_c_enc_c_dec": round(float(ar.score(x8, resample_poly(ycc, 1, 2), ar.CODEC_DELAY)[0]), 3)}
            print(json.dumps(r), flush=True)
            rows.append(r)
    if out:
        json.dump(rows, open(out, "w"), indent=1)


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("clips", nargs="+")
    a.add_argument("--modes", default="600,400")
    a.add_argument("--lag", type=int, default=10)
    a.add_argument("--loss", type=float, default=0.0)
    a.add_argument("--every", type=int, default=0, help="lose one superframe in EVERY (the K6 XL test)")
    a.add_argument("--seconds", type=float, default=0.0)
    a.add_argument("--json")
    a = a.parse_args()
    main(a.clips, a.modes.split(","), a.lag, a.loss, a.every, a.seconds, a.json)
