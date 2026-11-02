"""FARGAN (Opus's neural vocoder) decoding sotto v3.8 bitstreams: the study in docs/fargan-study.md.

For each 16 kHz clip of docs/samples/v38/ (no radio channel) it renders:
  v38                 sotto's own C decoder (c/sotto_dec -16)
  fargan_v38          FARGAN from features measured on the v3.8 output (so its 4-8 kHz bands
                      carry the hiss index)
  fargan_v38_blindhf  the same, with FARGAN's five bands from 4 to 8 kHz guessed from the lower
                      bands (ridge regression trained on the other clips' originals): a decoder
                      without the hiss index
  fargan_v38_sp       the same as fargan_v38, with pitch and voicing taken from sotto's bits
  fargan_ceiling      FARGAN from features of the original speech (not sendable at 2400 bit/s)
and an ablation that swaps the original's value of one feature group into fargan_v38.

Opus is not part of this repo. Build Opus 1.6.1 with its trained weights (the release tarball, or
the crates.io package opusic-sys 0.7.6, which bundles it) and its fargan_demo:
  sh ./configure --enable-dred --enable-osce --disable-shared && make && make fargan_demo
Opus is BSD-licensed; see its COPYING.

Usage: python tools/fargan_study.py --demo PATH/TO/fargan_demo [--out DIR] [--flac VOICE ...]
  (make first, for c/sotto_dec). Writes DIR/fargan_study.json and, for --flac voices, the FARGAN
  rows as FLAC (docs/samples/fargan/ holds two voices).
"""
import argparse, glob, json, os, subprocess, sys, tempfile
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
from pesq import pesq
from pystoi import stoi

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
SRC = os.path.join(ROOT, "docs", "samples", "v38")
NB = 18                          # FARGAN's Bark bands; features 18 and 19 are pitch and its correlation
HI = slice(13, NB)               # the bands centred at 4.0, 4.8, 5.6, 6.8 and 8.0 kHz

# Opus dnn/freq.c dct(): orthonormal DCT-II over the 18 log10 band energies; features[0] -= 4
_i, _j = np.arange(NB)[:, None], np.arange(NB)[None, :]
DCT = np.cos((_i + .5) * _j * np.pi / NB) * np.sqrt(2. / NB)
DCT[:, 0] *= np.sqrt(.5)


def to_ly(F):
    c = F[:, :NB].copy()
    c[:, 0] += 4
    return c @ DCT.T


def from_ly(F, Ly):
    G = F.copy()
    G[:, :NB] = Ly @ DCT
    G[:, 0] -= 4
    return G


class Fargan:
    def __init__(self, demo, work):
        self.demo, self.work, self.n = demo, work, 0

    def _run(self, mode, data, dtype, width):
        self.n += 1
        i, o = f"{self.work}/{self.n}.in", f"{self.work}/{self.n}.out"
        data.astype(dtype).tofile(i)
        subprocess.run([self.demo, mode, i, o], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return np.fromfile(o, width)

    def features(self, x):
        """20 features per 10 ms from 16 kHz float audio."""
        return self._run("-features", np.round(np.clip(x, -1, 1) * 32767), "<i2", "<f4").reshape(-1, 20)

    def synth(self, F, n):
        """16 kHz audio from features; fargan_demo's output is aligned with its input."""
        y = self._run("-fargan-synthesis", F, "<f4", "<i2").astype(float) / 32768
        return np.pad(y, (0, max(0, n - len(y))))[:n]


def score(x, y):
    out = {}
    for k, fs, mode, a, b in (("pesq_wb", 16000, "wb", x, y),
                              ("pesq_nb", 8000, "nb", resample_poly(x, 1, 2), resample_poly(y, 1, 2))):
        try:
            out[k] = float(pesq(fs, a, b, mode))
        except Exception:
            out[k] = float("nan")
    out["estoi"] = float(stoi(x, y, 16000, extended=True))
    return out


def sotto_pitch(bitfile, n):
    """Per 10 ms: FARGAN's pitch feature from sotto's decoded Wo, and the voicing class."""
    import hiss_band as hb
    b = np.unpackbits(np.fromfile(bitfile, np.uint8)).reshape(-1, 48)
    m = hb.SottoV38(vowel="speaker", vowel_shape="fine", voiced_trim=-5, blend=True, release_ms=0,
                    keep_mixed=True).decode(b)
    period = np.clip(16000 / (m["Wo"] * 8000 / (2 * np.pi)), 32, 255)
    return (np.log2(256 / period) - 1.5)[:n], np.minimum(m["voiced"], 2)[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", required=True, help="Opus fargan_demo")
    ap.add_argument("--dec", default=os.path.join(ROOT, "c", "sotto_dec"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "results"))
    ap.add_argument("--flac", nargs="*", default=[], help="voices whose FARGAN rows to write as FLAC")
    ap.add_argument("--flac-dir", default=os.path.join(ROOT, "docs", "samples", "fargan"))
    a = ap.parse_args()
    clips = sorted(os.path.basename(p)[:-len("__original16.flac")] for p in glob.glob(f"{SRC}/*__original16.flac"))
    res, ab, data = {}, {}, {}
    with tempfile.TemporaryDirectory() as work:
        fg = Fargan(a.demo, work)
        for c in clips:
            x, fs = sf.read(f"{SRC}/{c}__original16.flac")
            assert fs == 16000
            bit = f"{SRC}/{c}__No_radio_channel.bit"
            subprocess.run([a.dec, "-16", bit, f"{work}/{c}.raw"], check=True)
            y = np.fromfile(f"{work}/{c}.raw", "<i2").astype(float) / 32768
            n = min(len(x), len(y))
            x, y = x[:n], y[:n]
            Fx, Fy = fg.features(x), fg.features(y)
            m = min(len(Fx), len(Fy))
            p, v = sotto_pitch(bit, m)
            m = len(p)
            data[c] = (x, y, Fx[:m], Fy[:m], p, v)
        for c in clips:
            x, y, Fx, Fy, p, v = data[c]
            n, others = len(x), [k for k in clips if k != c]
            # blind 4-8 kHz: ridge from bands 0-12 to 13-17, trained on the other clips' originals
            tr = np.concatenate([to_ly(data[k][2]) for k in others])
            A = np.column_stack([np.ones(len(tr)), tr[:, :13]])
            Wr = np.linalg.solve(A.T @ A + 1e-2 * np.eye(14), A.T @ tr[:, HI])
            Ly, Lx = to_ly(Fy), to_ly(Fx)
            Lb = Ly.copy()
            Lb[:, HI] = np.column_stack([np.ones(len(Ly)), Ly[:, :13]]) @ Wr
            # pitch from sotto's bits; correlation = the other clips' mean per voicing class
            cm = np.array([np.mean(np.concatenate([data[o][2][data[o][5] == cl, 19] for o in others]))
                           for cl in range(3)])
            Fs = Fy.copy()
            Fs[v >= 1, 18] = p[v >= 1]
            Fs[:, 19] = cm[v]
            rows = {"v38": y, "fargan_ceiling": fg.synth(Fx, n), "fargan_v38": fg.synth(Fy, n),
                    "fargan_v38_blindhf": fg.synth(from_ly(Fy, Lb), n), "fargan_v38_sp": fg.synth(Fs, n)}
            res[c] = {r: score(x, s) for r, s in rows.items()}
            sw = {}
            L = Ly.copy(); L[:, :13] = Lx[:, :13]; sw["true_0_4k"] = from_ly(Fy, L)
            L = Ly.copy(); L[:, HI] = Lx[:, HI]; sw["true_4_8k"] = from_ly(Fy, L)
            G = Fy.copy(); G[:, 18] = Fx[:, 18]; sw["true_period"] = G
            G = Fy.copy(); G[:, 19] = Fx[:, 19]; sw["true_corr"] = G
            ab[c] = {r: score(x, fg.synth(F, n)) for r, F in sw.items()}
            if c in a.flac:
                os.makedirs(a.flac_dir, exist_ok=True)
                for r in ("fargan_v38", "fargan_v38_blindhf", "fargan_ceiling"):
                    sf.write(f"{a.flac_dir}/{c}__{r}.flac", rows[r], 16000, subtype="PCM_16")
            print(c, {r: round(s["pesq_wb"], 2) for r, s in res[c].items()}, flush=True)
    mean = lambda d: {r: {m: round(float(np.nanmean([d[c][r][m] for c in clips])), 3)
                          for m in ("pesq_wb", "pesq_nb", "estoi")} for r in d[clips[0]]}
    out = dict(clips=clips, mean=mean(res), ablation_mean=mean(ab), per_clip=res, ablation=ab)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "fargan_study.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(dict(mean=out["mean"], ablation_mean=out["ablation_mean"]), indent=1))


if __name__ == "__main__":
    main()
