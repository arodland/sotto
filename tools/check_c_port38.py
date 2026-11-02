"""Check the v3.8 C port (c/, VARIANT=v3.8) against the Python model (sim/hiss_band.py SottoV38).

Usage: python tools/check_c_port38.py CLIP16K.wav [...] [--lag 10] [--loss 0.1] [--seconds 6] [--json OUT.json]

Encoder: both encoders see the same 16 kHz samples; the Python runs the fixed-lag tracker
(TRACK2["lag"] = 8) and judges whisper runs on their first lag + 1 frames (whisper_lag), as
the C does with SOTTO_RULE_LAG = lag. Reports the fraction of frames whose 48 bits match and
which fields differ (voicing, index, side-channel bit, energy, shape), plus the same for the
Python with whole-run whisper judgement (the sound Jeff approved).
Decoder: both decoders get the Python's bits (and the same loss pattern). The C output is
delayed by SOTTO_DEC16_DELAY_FRAMES frames, which the sotto_dec tool removes. Reports the
0-4 kHz sample SNR between the two (the noise band has random phase, so it is compared by
its 4-8 kHz level per 10 ms frame: mean and 95th percentile of the absolute dB difference
over frames above -60 dB), and PESQ-NB of each against the original.
Build first: make -C c (VARIANT=v3.8 is the default; RULE_LAG=... to change the look-ahead).
"""
import argparse, json, os, subprocess, sys, tempfile
import numpy as np, soundfile as sf
from scipy.signal import resample_poly

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
import vocoder_lab as vl                                       # noqa: E402
import hiss_band as hb                                         # noqa: E402
import audio_race as ar                                        # noqa: E402

ENC = dict(vowel="speaker", whisper=True, vsmooth=True, sib_db=-10.0, sib_voiced=True)
DEC = dict(vowel="speaker", vowel_shape="fine", voiced_trim=-5, blend=True, release_ms=0, keep_mixed=True)
FIELDS = [("voicing", 0, 2), ("index", 2, 8), ("side", 8, 9), ("energy", 9, 14), ("shape", 14, 48)]


def c_encode(x16):
    with tempfile.TemporaryDirectory() as d:
        x16.astype("<i2").tofile(f"{d}/in.raw")
        subprocess.run([f"{ROOT}/c/sotto_enc", "-16", f"{d}/in.raw", f"{d}/out.bit"], check=True)
        b = np.fromfile(f"{d}/out.bit", np.uint8)
    return np.unpackbits(b).reshape(-1, 48).astype(np.int8)


def c_decode(bits, good=None):
    with tempfile.TemporaryDirectory() as d:
        np.packbits(np.asarray(bits, np.uint8).reshape(-1, 48), axis=1).tofile(f"{d}/in.bit")
        args = [f"{ROOT}/c/sotto_dec", "-16", f"{d}/in.bit", f"{d}/out.raw"]
        if good is not None:
            np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
            args.append(f"{d}/good.u8")
        subprocess.run(args, check=True)
        return np.fromfile(f"{d}/out.raw", "<i2").astype(float) / 32768


def snr_db(ref, y):
    n = min(len(ref), len(y))
    return 10 * np.log10(np.sum(ref[:n] ** 2) / (np.sum((ref[:n] - y[:n]) ** 2) + 1e-20))


def band_track(y, lo=4000, hi=8000):
    P = np.abs(hb.stft(y)) ** 2
    return hb.bands_db(P, [(lo, hi)])[:, 0]


def compare_bits(bp, bc):
    n = min(len(bp), len(bc))
    same = np.all(bp[:n] == bc[:n], 1)
    diff = {name: int(np.sum(np.any(bp[:n, a:b] != bc[:n, a:b], 1))) for name, a, b in FIELDS}
    return round(float(same.mean()), 4), diff


def main(clips, lag, loss, seconds, out):
    vl.TRACK2["lag"] = 8
    enc_lag = hb.SottoV38(whisper_lag=lag, **ENC)
    enc_full = hb.SottoV38(**ENC)
    dec = hb.SottoV38(**DEC)
    rows = []
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
        bp = enc_lag.encode(xq)
        bfull = enc_full.encode(xq)
        bc = c_encode(x16)
        exact, diff = compare_bits(bp, bc)
        exact_full, diff_full = compare_bits(bfull, bc)
        good = None
        if loss:
            good = np.random.default_rng(1).random(len(bp)) >= loss
            good[0] = True
        yp = dec.decode_audio16(bp, good, n16=len(xq))
        yc = c_decode(bp, good)
        n = min(len(yp), len(yc))
        lo_p, lo_c = resample_poly(yp[:n], 1, 2), resample_poly(yc[:n], 1, 2)
        hp, hc = band_track(yp[:n]), band_track(yc[:n])
        live = hp > -60
        d = np.abs(hp - hc)[live]
        x8 = resample_poly(xq, 1, 2)
        r = {"clip": os.path.basename(path), "frames": [len(bp), len(bc)],
             "enc_bit_exact_vs_lag": exact, "field_mismatch_vs_lag": diff,
             "enc_bit_exact_vs_full": exact_full, "field_mismatch_vs_full": diff_full,
             "dec_len": [len(yp), len(yc)], "dec_low_snr_db": round(float(snr_db(lo_p, lo_c)), 1),
             "dec_hiss_level_diff_db": [round(float(d.mean()), 2), round(float(np.percentile(d, 95)), 2)] if len(d) else None,
             "pesq_py": round(float(ar.score(x8, lo_p, ar.CODEC_DELAY)[0]), 3),
             "pesq_c": round(float(ar.score(x8, lo_c, ar.CODEC_DELAY)[0]), 3),
             "pesq_c_enc_c_dec": round(float(ar.score(x8, resample_poly(c_decode(bc, good), 1, 2), ar.CODEC_DELAY)[0]), 3)}
        print(json.dumps(r), flush=True)
        rows.append(r)
    if out:
        json.dump(rows, open(out, "w"), indent=1)


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("clips", nargs="+")
    a.add_argument("--lag", type=int, default=10)
    a.add_argument("--loss", type=float, default=0.0)
    a.add_argument("--seconds", type=float, default=0.0)
    a.add_argument("--json")
    a = a.parse_args()
    main(a.clips, a.lag, a.loss, a.seconds, a.json)
