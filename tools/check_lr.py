"""Check the sotto 1200 C port (c/sotto_lr.c) against the Python reference (sim/sotto_lr.py
Sotto1200, with the C encoder's fixed-lag tracker, TRACK2["lag"] = 8).

Usage: python tools/check_lr.py CLIP.flac [...] [--loss 0.1] [--json OUT.json]

Encoder: the fraction of 40 ms superframes whose 48 bits match exactly, and which fields differ.
Decoder: both decoders get the same (Python) bits, with a random superframe-loss pattern when
--loss is set; reports the sample SNR of the C output against the Python output and PESQ.
Build c/ first (make -C c).
"""
import argparse, json, os, subprocess, sys, tempfile
import numpy as np, soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
import vocoder_lab as vl                                       # noqa: E402
import sotto_lr as lr                                          # noqa: E402


def c_encode(x16, exe=None):
    with tempfile.TemporaryDirectory() as d:
        x16.astype("<i2").tofile(f"{d}/in.raw")
        subprocess.run([exe or f"{ROOT}/c/sotto_enc", "-1200", f"{d}/in.raw", f"{d}/out.bit"], check=True)
        b = np.fromfile(f"{d}/out.bit", np.uint8)
    return np.unpackbits(b).reshape(-1, 48).astype(np.int8)


def c_decode(bits, good=None, exe=None):
    with tempfile.TemporaryDirectory() as d:
        np.packbits(np.asarray(bits, np.uint8).reshape(-1, 48), axis=1).tofile(f"{d}/in.bit")
        args = [exe or f"{ROOT}/c/sotto_dec", "-1200", f"{d}/in.bit", f"{d}/out.raw"]
        if good is not None:
            np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
            args.append(f"{d}/good.u8")
        subprocess.run(args, check=True)
        return np.fromfile(f"{d}/out.raw", "<i2").astype(float) / 32768


def snr_db(ref, y):
    n = min(len(ref), len(y))
    return 10 * np.log10(np.sum(ref[:n] ** 2) / (np.sum((ref[:n] - y[:n]) ** 2) + 1e-20))


def fields(c):
    out, p = [], 0
    for k, w in c.layout():
        out.append((k, p, p + w))
        p += w
    return out


def main(clips, loss, out, dec_exe=None):
    vl.TRACK2["lag"] = 8
    v = lr.Sotto1200()
    import audio_race as ar
    rows = []
    for path in clips:
        x, fs = sf.read(path)
        assert fs == 8000
        x = x[:len(x) // 80 * 80]
        x16 = np.round(x * 32767).astype(np.int16)
        xq = x16 / 32767.0
        bp = v.encode(xq)
        bc = c_encode(x16)
        n = min(len(bp), len(bc))
        same = np.all(bp[:n] == bc[:n], 1)
        diff = {k: int(np.sum(np.any(bp[:n, a:b] != bc[:n, a:b], 1))) for k, a, b in fields(v)}
        good = None
        if loss:
            good = np.random.default_rng(1).random(len(bp)) >= loss
            good[0] = True
        yp = vl.synthesise(v.decode(bp, good))
        yc = c_decode(bp, good, dec_exe)
        r = {"clip": os.path.basename(path), "superframes": int(n), "superframes_py": len(bp),
             "superframes_c": len(bc), "bit_exact": round(float(same.mean()), 4),
             "field_mismatch": {k: d for k, d in diff.items() if d},
             "dec_snr_db": round(float(snr_db(yp, yc)), 1), "dec_len": [len(yp), len(yc)],
             "pesq_py_dec": round(float(ar.score(x, yp, ar.CODEC_DELAY)[0]), 3),
             "pesq_c_dec": round(float(ar.score(x, yc, ar.CODEC_DELAY)[0]), 3),
             "pesq_c_enc_c_dec": round(float(ar.score(x, c_decode(bc, good, dec_exe), ar.CODEC_DELAY)[0]), 3)}
        rows.append(r)
        print(json.dumps(r), flush=True)
    tot = sum(r["superframes"] for r in rows)
    summ = {"superframes": tot,
            "bit_exact": round(sum(r["bit_exact"] * r["superframes"] for r in rows) / tot, 4),
            "dec_snr_db_min": min(r["dec_snr_db"] for r in rows), "loss": loss}
    print(json.dumps(summ))
    if out:
        with open(out, "w") as f:
            json.dump({"summary": summ, "clips": rows}, f, indent=1)


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("clips", nargs="+")
    a.add_argument("--loss", type=float, default=0.0)
    a.add_argument("--json")
    a.add_argument("--dec", help="decoder executable (e.g. c/fixed/sotto_dec_fx)")
    a = a.parse_args()
    main(a.clips, a.loss, a.json, a.dec)
