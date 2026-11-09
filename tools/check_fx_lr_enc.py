#!/usr/bin/env python3
"""Compare the fixed-point sotto 1200 encoder (c/fixed/sotto_enc_fx -1200) with the float one
(c/sotto_enc -1200): per-field and whole-superframe agreement, and the SNR between the two
streams decoded by the same decoder. Also checks the constants the integer port assumes.

usage: python tools/check_fx_lr_enc.py CLIP.raw|CLIP.flac ... [--dec c/fixed/sotto_dec_fx] [--score]
--score adds PESQ-NB / ESTOI of each decoded stream against the clip (needs pesq, pystoi).
Clips: 8 kHz 16-bit mono raw, or anything ffmpeg reads (resampled to 8 kHz).
SPDX-License-Identifier: MIT"""
import argparse, os, re, subprocess, sys, tempfile
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDTHS = [2, 7, 5, 9, 9, 2, 3, 3, 8]
NAMES = ["voicing A", "pitch A", "energy A", "shape A1", "shape A2", "voicing B", "pitch B",
         "energy B", "shape B"]

def constants_ok():
    h = open(os.path.join(ROOT, "c", "sotto_lr_tables.h")).read()
    get = lambda n: float(re.search(rf"#define {n} ([0-9.]+)", h).group(1))
    return get("LR_JOINT") == 1.0 and get("LR_BG") == 1.5 and get("LR_NA") == 2 and get("LR_BST") == 2

def fields(bits):
    out = []
    for i in range(0, len(bits) // 6 * 6, 6):
        v = int.from_bytes(bits[i:i + 6], "big"); pos = 48; f = []
        for w in WIDTHS:
            pos -= w; f.append((v >> pos) & ((1 << w) - 1))
        out.append(f)
    return np.array(out)

def raw8k(path, tmp):
    if path.endswith(".raw"): return path
    out = os.path.join(tmp, os.path.basename(path) + ".raw")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-ar", "8000", "-ac", "1",
                    "-f", "s16le", out], check=True)
    return out

def best_lag(ref, out):
    """the output's delay against the clip, within 0.3 s (envelope correlation: a vocoder's
    waveform isn't phase-coherent with its input)"""
    env = lambda v: np.sqrt(np.convolve(v * v, np.ones(80) / 80, "same"))
    a, b = env(ref), env(out); n = min(len(a), len(b)) - 2400
    c = [np.dot(a[:n] - a[:n].mean(), b[l:l + n] - b[l:l + n].mean()) for l in range(2400)]
    return int(np.argmax(c))

def quality(ref, out, lag):
    """PESQ-NB and ESTOI at the given delay (as sim/audio_race.score)"""
    from pesq import pesq
    from pystoi import stoi
    m = min(len(ref), len(out) - lag)
    r, x = ref[:m], out[lag:lag + m]
    return pesq(8000, r, x, "nb"), stoi(r, x, 8000, extended=True)

def run(exe, *args):
    subprocess.run([os.path.join(ROOT, exe), *args], check=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--dec", default="c/fixed/sotto_dec_fx")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if not constants_ok():
        sys.exit("sotto_lr_tables.h changed: the fixed encoder assumes LR_JOINT 1.0, LR_BG 1.5, LR_NA 2, LR_BST 2")
    tot = np.zeros(len(WIDTHS)); n_all = n_same = 0; qs = []
    with tempfile.TemporaryDirectory() as tmp:
        for clip in a.clips:
            r = raw8k(clip, tmp)
            fb, xb = os.path.join(tmp, "f.bit"), os.path.join(tmp, "x.bit")
            run("c/sotto_enc", "-1200", r, fb); run("c/fixed/sotto_enc_fx", "-1200", r, xb)
            F, X = fields(open(fb, "rb").read()), fields(open(xb, "rb").read())
            n = min(len(F), len(X)); F, X = F[:n], X[:n]
            same = (F == X).all(1).sum(); per = (F == X).mean(0)
            fo, xo = os.path.join(tmp, "f.raw"), os.path.join(tmp, "x.raw")
            run(a.dec, "-1200", fb, fo); run(a.dec, "-1200", xb, xo)
            y1, y2 = np.fromfile(fo, np.int16).astype(float), np.fromfile(xo, np.int16).astype(float)
            m = min(len(y1), len(y2)); y1, y2 = y1[:m], y2[:m]
            snr = 10 * np.log10((y1 ** 2).sum() / max(((y1 - y2) ** 2).sum(), 1e-9))
            print(f"{os.path.basename(clip)}: {n} superframes, {100 * same / n:5.1f}% identical, "
                  f"decoded float vs fixed stream SNR {snr:5.1f} dB")
            print("    " + "  ".join(f"{nm} {100 * p:5.1f}" for nm, p in zip(NAMES, per)))
            if a.score:
                ref = np.fromfile(r, np.int16).astype(float)
                lag = best_lag(ref, y1)                      # one delay for both: the same decoder
                (pf, ef), (px, ex) = quality(ref, y1, lag), quality(ref, y2, lag)
                print(f"    PESQ / ESTOI: float encoder {pf:.3f} / {ef:.3f}, fixed encoder {px:.3f} / {ex:.3f}")
                qs.append((pf, ef, px, ex))
            tot += per * n; n_all += n; n_same += same
    print(f"all: {n_all} superframes, {100 * n_same / n_all:.1f}% identical; per field: " +
          "  ".join(f"{nm} {100 * t / n_all:.1f}" for nm, t in zip(NAMES, tot)))
    if qs:
        q = np.mean(qs, 0)
        print(f"mean PESQ / ESTOI: float encoder {q[0]:.3f} / {q[1]:.3f}, fixed encoder {q[2]:.3f} / {q[3]:.3f}")

if __name__ == "__main__":
    main()
