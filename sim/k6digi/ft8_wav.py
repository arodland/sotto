"""Decode an FT8 WAV with the K6 receiver core (c/k6/ft8/ft8k.c) on the host, the way a K6
would see one window of it, stepping the window across the band. For checking the decoder
against WSJT-X's own decodes (ft8_lib's test recordings) and for the SNR sweeps.

  python3 -m sim.k6digi.ft8_wav FILE.wav [--lo 200 --hi 3000 --nb 32]

Prints every decode, one line each in time order: "dt (s from the file's start) f0 (Hz) snr text",
so a message sent in several slots is listed once a slot.

MIT licence."""
import argparse
import os
import subprocess
import tempfile

import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
FT8 = os.path.join(ROOT, "c", "k6", "ft8")
BUILD = os.path.join(HERE, "build")


def host():
    exe = os.path.join(BUILD, "ft8k_host")
    srcs = [os.path.join(FT8, n) for n in ("ft8k_host.c", "ft8k.c", "ft8k.h", "ft8k_tables.h")]
    if not os.path.exists(exe) or os.path.getmtime(exe) < max(map(os.path.getmtime, srcs)):
        os.makedirs(BUILD, exist_ok=True)
        tmp = exe + ".%d" % os.getpid()
        subprocess.run(["cc", "-O2", "-o", tmp, srcs[0], srcs[1]], check=True)
        os.replace(tmp, exe)
    return exe


def to_iq(x, fs, fc, fd=200.0, rms=60.0):
    """real audio x at fs -> complex int16 at fd around fc, noise-ish rms `rms`"""
    n = np.arange(len(x))
    z = x * np.exp(-2j * np.pi * fc / fs * n)
    from fractions import Fraction
    r = Fraction(fd / fs).limit_denominator(1000)
    z = signal.resample_poly(z, r.numerator, r.denominator, window=("kaiser", 8.0))
    z = z / (np.sqrt(np.mean(np.abs(z) ** 2)) + 1e-12) * rms
    z = np.clip(np.round(z), -8000, 8000)
    out = np.empty(2 * len(z), np.int16)
    out[0::2], out[1::2] = z.real, z.imag
    return out


def run_iq(iq, nb, opts=()):
    with tempfile.NamedTemporaryFile(suffix=".iq", delete=False) as f:
        f.write(iq.tobytes())
        name = f.name
    try:
        out = subprocess.run([host(), "iq", name, str(nb)] + list(opts), capture_output=True,
                             text=True, check=True).stdout
    finally:
        os.unlink(name)
    res = []
    for line in out.splitlines():
        p = line.split(None, 5)
        res.append(dict(row=int(p[0]), bin=int(p[1]), snr=int(p[2]), score=int(p[3]),
                        iters=int(p[4]), text=p[5] if len(p) > 5 else ""))
    return res


def decode_all(x, fs, lo=200.0, hi=3000.0, nb=32, step=100.0, opts=()):
    """every decode in every window from lo to hi Hz, as [(dt, freq of tone 0, snr, text)] in
    time order. A transmission that two windows both hear (same text, within 2 s and 20 Hz) is
    listed once, at its best SNR; the same text again later (the next slot) is listed again."""
    found = []
    fd = 200.0
    for fc in np.arange(lo + nb / 4 * 6.25, hi, step):
        for d in run_iq(to_iq(x, fs, fc, fd), nb, opts):
            f0 = fc + (d["bin"] - nb // 2) * fd / 32
            dt = d["row"] * 0.08
            for i, (dt2, f2, snr2, t2) in enumerate(found):
                if t2 == d["text"] and abs(dt2 - dt) < 2.0 and abs(f2 - f0) < 20.0:
                    if d["snr"] > snr2:
                        found[i] = (dt, f0, d["snr"], d["text"])
                    break
            else:
                found.append((dt, f0, d["snr"], d["text"]))
    return sorted(found)


def decode_audio(x, fs, lo=200.0, hi=3000.0, nb=32, step=100.0, opts=()):
    """{text: (freq of tone 0, dt, snr)}, the first time each text is heard (decode_all lists them all)"""
    found = {}
    for dt, f0, snr, text in decode_all(x, fs, lo, hi, nb, step, opts):
        found.setdefault(text, (f0, dt, snr))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--lo", type=float, default=200)
    ap.add_argument("--hi", type=float, default=3000)
    ap.add_argument("--nb", type=int, default=32)
    ap.add_argument("opts", nargs="*")
    a = ap.parse_args()
    import wave
    w = wave.open(a.wav)
    fs = w.getframerate()
    x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(float)
    for dt, f0, snr, t in decode_all(x, fs, a.lo, a.hi, a.nb, opts=a.opts):
        print("%6.2f %4.0f %4d  %s" % (dt, f0, snr, t))


if __name__ == "__main__":
    main()
