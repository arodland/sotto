"""FT8 decode rate against SNR (dB in 2500 Hz, WSJT-X's measure) for the K6 receiver core,
on clean audio (the decoder alone) or through the K6's GP0 path (sim/k6digi/gp0.py).

  python3 -m sim.k6digi.ft8_sweep [--snr -12 -14 ...] [--trials 20] [--opts hann=0 ...]

Each trial: a random standard message, a random start (0.3-1.8 s) and a random base
frequency, so tones fall anywhere between the DFT's bins. MIT licence."""
import argparse
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import ft8ref, ft8_wav

CALLS = ["AG7EW", "W1AW", "K1ABC", "VE9QRP", "KA7ABC", "KD7XYZ", "W7AAA", "AA1BB"]
GRIDS = ["CN87", "FN42", "DM79", "EM10", "-10", "R-07", "RR73", "73"]


def trial(args):
    snr, seed, opts, nb, path = args
    rng = np.random.default_rng(seed)
    text = "%s %s %s" % (rng.choice(["CQ"] + CALLS), rng.choice(CALLS), rng.choice(GRIDS))
    b, tones = ft8ref.encode(text)
    want = ft8ref.unpack_text(b)
    fs = 12000
    f0 = 1500.0 + rng.uniform(-30, 30)
    start = rng.uniform(0.3, 1.8)
    al = os.environ.get("FT8_ALIGN", "")
    if "f" in al:                             # tones on bin centres
        f0 = 1520.0 - 3 * 6.25 + 6.25 * rng.integers(-4, 4)
    if "t" in al:                             # symbols on rows
        start = 0.08 * rng.integers(4, 22) + 0.16
    ph, env = ft8ref.gfsk_phase(tones, fs, f0)
    x = np.zeros(int(15.5 * fs))
    s = int(start * fs)
    x[s:s + len(ph)] = np.sqrt(2) * np.cos(ph + rng.uniform(0, 6.3)) * env   # power 1
    if path == "audio":
        sigma = np.sqrt(fs / 2 / 2500 / 10 ** (snr / 10))                    # 2500 Hz noise
        x += rng.normal(0, sigma, len(x))
        res = ft8_wav.run_iq(ft8_wav.to_iq(x, fs, 1520.0), nb, opts)
    else:                                       # "k6", "k6:FLOOR" or "k6:FLOOR:L"
        from . import gp0
        f = path.split(":")                    # k6[:FLOOR[:L]]
        floor = float(f[1]) if len(f) > 1 else 0.05
        L = int(f[2]) if len(f) > 2 else gp0.L
        res = gp0.run_ssb(x, fs, 1520.0, nb, opts, snr, seed=seed, floor=floor, L=L)
    return any(d["text"] == want for d in res)


def sweep(snrs, trials=20, opts=(), nb=32, path="audio", seed0=0):
    out = {}
    ft8_wav.host()
    with ProcessPoolExecutor() as ex:
        for snr in snrs:
            r = list(ex.map(trial, [(snr, seed0 + i, tuple(opts), nb, path) for i in range(trials)]))
            out[snr] = sum(r) / trials
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snr", type=float, nargs="*", default=[-12, -14, -16, -17, -18, -19, -20])
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--nb", type=int, default=32)
    ap.add_argument("--path", default="audio")
    ap.add_argument("--opts", nargs="*", default=[])
    a = ap.parse_args()
    r = sweep(a.snr, a.trials, a.opts, a.nb, a.path)
    print(" ".join(a.opts) or "default", a.path, "nb=%d" % a.nb,
          "  ".join("%+.0f:%3.0f%%" % (k, 100 * v) for k, v in r.items()))


if __name__ == "__main__":
    main()
