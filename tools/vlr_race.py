"""Score the very-low-rate sotto modes against Codec 2 and sotto 1200 on clean voices.

Usage: python tools/vlr_race.py [--samples DIR] [--codecs 600,400,lr1200,c2_700C,c2_1200]
       [--loss FRAC] [--json OUT.json] [--tables sim/data/vlr_tables.npz]

Mean PESQ-NB / ESTOI over the clips (default: the 16 held-out voices in
/mnt/project-files/vocoder/samples-clean/). sotto runs the Python reference with the
whole-utterance pitch tracker, as docs/sotto-1200.md's table did. Codec 2 700C has a longer
delay than the other codecs, so its lag is searched (audio_race.search_lag); the rest use
the shared CODEC_DELAY. --loss drops that fraction of superframes at random (seeded per clip);
--every 8 drops one superframe in 8, as one lost K6 XL packet in 8 does through the interleave.
"""
import argparse, glob, json, os, subprocess, sys, tempfile
from multiprocessing import Pool
import numpy as np, soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
import audio_race as ar                                        # noqa: E402
import sotto_lr as lr                                          # noqa: E402
import sotto_vlr as vr                                         # noqa: E402

C2 = os.path.join(ROOT, "build", "codec2", "src")
OPT = {}


def c2(x, mode):
    with tempfile.TemporaryDirectory() as d:
        np.round(x * 32767).astype("<i2").tofile(f"{d}/i.raw")
        subprocess.run([f"{C2}/c2enc", mode, f"{d}/i.raw", f"{d}/b"], check=True)
        subprocess.run([f"{C2}/c2dec", mode, f"{d}/b", f"{d}/o.raw"], check=True)
        return np.fromfile(f"{d}/o.raw", "<i2") / 32768


def run(name, x, seed):
    loss = OPT.get("loss", 0.0)
    if name.startswith("c2_"):
        return c2(x, name[3:]), None
    if name == "lr1200":
        v = lr.Sotto1200()
    else:
        t = dict(np.load(OPT["tables"])) if OPT.get("tables") else None
        v = vr.SottoVLR(name, tables=t)
    bits = v.encode(x)
    good = None
    if OPT.get("every"):
        good = np.arange(len(bits)) % OPT["every"] != 3
    elif loss:
        good = np.random.default_rng(seed).random(len(bits)) >= loss
    return v.decode_audio(bits, good), good


def job(args):
    name, path = args
    x, fs = sf.read(path)
    assert fs == 8000
    seed = sum(map(ord, os.path.basename(path)))
    y, _ = run(name, x, seed)
    lag = ar.search_lag(x, y) if name == "c2_700C" else ar.CODEC_DELAY
    p, e = ar.score(x, y, lag)
    return name, os.path.splitext(os.path.basename(path))[0], float(p), float(e)


def init(opt):
    OPT.update(opt)


def main(samples, codecs, loss, tables, out, every=0):
    clips = sorted(glob.glob(os.path.join(samples, "*.flac")))
    opt = dict(loss=loss, tables=tables, every=every)
    with Pool(4, initializer=init, initargs=(opt,)) as pool:
        r = pool.map(job, [(c, p) for c in codecs for p in clips])
    res = {}
    for name, clip, p, e in r:
        res.setdefault(name, {})[clip] = dict(pesq=round(p, 3), estoi=round(e, 3))
    summary = {}
    for name in codecs:
        v = res[name].values()
        summary[name] = dict(pesq=round(float(np.mean([a["pesq"] for a in v])), 3),
                             estoi=round(float(np.mean([a["estoi"] for a in v])), 3))
        print(f"{name:10s} PESQ {summary[name]['pesq']:.3f}  ESTOI {summary[name]['estoi']:.3f}", flush=True)
    if out:
        with open(out, "w") as f:
            json.dump(dict(loss=loss, every=every, summary=summary, per_clip=res), f, indent=1)
    return summary


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--samples", default="/mnt/project-files/vocoder/samples-clean")
    a.add_argument("--codecs", default="600,400,lr1200,c2_700C,c2_1200")
    a.add_argument("--loss", type=float, default=0.0)
    a.add_argument("--every", type=int, default=0,
                   help="lose superframe 3 of every EVERY (one K6 XL packet in EVERY, interleaved)")
    a.add_argument("--tables")
    a.add_argument("--json")
    a = a.parse_args()
    main(a.samples, a.codecs.split(","), a.loss, a.tables, a.json, a.every)
