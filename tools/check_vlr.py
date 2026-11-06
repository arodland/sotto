"""Check the fixed-point very-low-rate decoder (c/fixed/sotto_fx_vlr.c, sotto_vlr_dec_fx)
against the Python reference (sim/sotto_vlr.py) on the same bits.

Usage: python tools/check_vlr.py CLIP.flac [...] [--mode 600|400] [--loss FRAC | --every 8]
       [--json OUT.json] [--bits-dir DIR]

Both decoders get the Python encoder's bits (and the same loss pattern). Reports the C output's
sample SNR against the Python output (after finding their offset, which is the v3.7 core's
one-frame delay) and both PESQ-NB scores against the input. --bits-dir also keeps the .bit
files, for the Cortex-M0 bench. Build first: make -C c VARIANT=v3.7 all fixed.
"""
import argparse, json, os, subprocess, sys, tempfile
import numpy as np, soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
import audio_race as ar                                        # noqa: E402
import sotto_vlr as vr                                         # noqa: E402

EXE = os.path.join(ROOT, "c", "fixed", "sotto_vlr_dec_fx")


def c_decode(mode, bits, good=None, exe=EXE):
    with tempfile.TemporaryDirectory() as d:
        np.packbits(np.asarray(bits, np.uint8).reshape(-1, 48), axis=1).tofile(f"{d}/in.bit")
        args = [exe, mode, f"{d}/in.bit", f"{d}/out.raw"]
        if good is not None:
            np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
            args.append(f"{d}/good.u8")
        subprocess.run(args, check=True)
        return np.fromfile(f"{d}/out.raw", "<i2").astype(float) / 32768


def offset(ref, y, max_lag=400):
    """Lag of y behind ref (samples) maximising their correlation."""
    n = min(len(ref), len(y)) - max_lag
    best = max((np.dot(ref[:n], y[l:l + n]), l) for l in range(max_lag))
    return best[1]


def snr_db(ref, y):
    n = min(len(ref), len(y))
    return 10 * np.log10(np.sum(ref[:n] ** 2) / (np.sum((ref[:n] - y[:n]) ** 2) + 1e-20))


def main(clips, mode, loss, every, out, bits_dir):
    v = vr.SottoVLR(mode)
    rows = []
    for path in clips:
        x, fs = sf.read(path)
        assert fs == 8000
        x = np.round(x * 32767) / 32768
        bits = v.encode(x)
        good = None
        if every:
            good = np.arange(len(bits)) % every != 3
        elif loss:
            good = np.random.default_rng(1).random(len(bits)) >= loss
        yp = v.decode_audio(bits, good)
        yc = c_decode(mode, bits, good)
        lag = offset(yp, yc)
        yc = yc[lag:]
        name = os.path.splitext(os.path.basename(path))[0]
        if bits_dir:
            os.makedirs(bits_dir, exist_ok=True)
            np.packbits(bits.astype(np.uint8), axis=1).tofile(os.path.join(bits_dir, f"{name}_{mode}.bit"))
        r = dict(clip=name, superframes=len(bits), c_lag=lag, snr_db=round(float(snr_db(yp, yc)), 1),
                 pesq_py=round(float(ar.score(x, yp, ar.CODEC_DELAY)[0]), 3),
                 pesq_c=round(float(ar.score(x, yc, ar.CODEC_DELAY)[0]), 3))
        print(json.dumps(r), flush=True)
        rows.append(r)
    s = dict(mode=mode, loss=loss, every=every, min_snr_db=min(r["snr_db"] for r in rows),
             pesq_py=round(float(np.mean([r["pesq_py"] for r in rows])), 3),
             pesq_c=round(float(np.mean([r["pesq_c"] for r in rows])), 3))
    print(json.dumps(s))
    if out:
        json.dump(dict(summary=s, clips=rows), open(out, "w"), indent=1)
    return s


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("clips", nargs="+")
    a.add_argument("--mode", default="600")
    a.add_argument("--loss", type=float, default=0.0)
    a.add_argument("--every", type=int, default=0)
    a.add_argument("--json")
    a.add_argument("--bits-dir")
    a = a.parse_args()
    main(a.clips, a.mode, a.loss, a.every, a.json, a.bits_dir)
