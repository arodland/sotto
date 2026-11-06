#!/usr/bin/env python3
"""Host side of the K6 GP0 modules (c/k6/mod/gp0fm.c, gp0raw.c): parse their `x` dump lines
from a K6 serial log and look at the receive IF they hold (docs/k6-fd6818-audio.md).

  python3 tools/k6_gp0.py fm  LOG [--png out.png]   gp0fm: FM-demodulate each capture
  python3 tools/k6_gp0.py rate LOG                   gp0raw: the IF's quality if the raw
                                                     22.9 MS/s stream were sampled k times
                                                     slower (DMA at a lower rate, say)

The log is the K6's serial output (`K6 MOD say ...` lines or bare module lines). The sample rate
comes from the module's `fm start ... bytes=N cal caps=10 t=T` line: T ticks of 100 us for 10
captures of N bytes. MIT licence."""
import argparse
import re
import sys

import numpy as np


def parse(path):
    """-> (bytes per capture, seconds per capture, {capture: bytes})"""
    caps, n, t = {}, None, None
    for ln in open(path, errors="replace"):
        m = re.search(r"fm start floor=\S+ bytes=(\d+) cal caps=(\d+) t=(\d+)", ln)
        if m:
            n, t = int(m.group(1)), int(m.group(3)) * 100e-6 / int(m.group(2))
            caps = {}
            continue
        m = re.search(r"(?:^|say )x (\d+) ([0-9a-f]{4}) ([0-9a-f]+)\s*$", ln)
        if m:
            k, off, hx = int(m.group(1)), int(m.group(2), 16), m.group(3)
            buf = caps.setdefault(k, bytearray())
            if off == len(buf):
                buf += bytes.fromhex(hx)
    if n is None:
        sys.exit("no 'fm start' line in the log")
    return n, t, {k: bytes(v) for k, v in caps.items()}


def band(x, fs, lo, hi):
    """x band-passed lo..hi Hz, analytic (an FFT mask: one-sided, so the result is complex)"""
    X = np.fft.fft(x - np.mean(x))
    f = np.fft.fftfreq(len(x), 1 / fs)
    X[(f < lo) | (f > hi)] = 0
    return 2 * np.fft.ifft(X)


def inst_freq(z, fs):
    return np.diff(np.unwrap(np.angle(z))) * fs / (2 * np.pi)


def demod(x, fs, lo=8000, hi=26000, audio=3000):
    """-> (mean IF Hz, audio deviation in Hz at fs, with 10% cut at each end)"""
    f = inst_freq(band(np.asarray(x, float), fs, lo, hi), fs)
    F = np.fft.rfft(f - f.mean())
    F[np.fft.rfftfreq(len(f), 1 / fs) > audio] = 0
    a = np.fft.irfft(F, len(f))
    cut = len(a) // 10
    return f.mean(), a[cut:len(a) - cut]


def tone(a, fs):
    """the strongest audio component (Hz) and its amplitude"""
    w = np.hanning(len(a))
    A = np.abs(np.fft.rfft(a * w, 8 * len(a)))
    f = np.fft.rfftfreq(8 * len(a), 1 / fs)
    i = np.argmax(A[f > 100]) + np.count_nonzero(f <= 100)
    return f[i], 2 * A[i] / w.sum()


def cmd_fm(args):
    n, t, caps = parse(args.log)
    fs = n / t
    print(f"gp0fm: {n} bytes a capture, {fs / 1e3:.2f} k bytes/s")
    rows = []
    for k, b in sorted(caps.items()):
        x = np.frombuffer(b, np.uint8).astype(float)
        mif, a = demod(x, fs)
        ft, amp = tone(a, fs)
        print(f"capture {k}: {len(b)} B, mean IF {mif / 1e3:.3f} kHz, audio rms {a.std():.0f} Hz,"
              f" peak {np.percentile(np.abs(a), 99):.0f} Hz, strongest {ft:.1f} Hz at {amp:.0f} Hz")
        rows.append((k, a))
    if args.png:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(len(rows), 1, figsize=(8, 2.5 * len(rows)), squeeze=False)
        for (k, a), axk in zip(rows, ax[:, 0]):
            axk.plot(np.arange(len(a)) / fs * 1e3, a / 1e3)
            axk.set_ylabel("kHz")
            axk.set_title(f"capture {k}: demodulated")
        ax[-1, 0].set_xlabel("ms")
        fig.tight_layout()
        fig.savefig(args.png, dpi=120)
        print("wrote", args.png)


def bits_of(b):
    w = np.frombuffer(b[:len(b) // 4 * 4], "<u4")
    return ((w[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()


def cmd_rate(args):
    n, t, caps = parse(args.log)
    fs = n * 8 / t
    print(f"gp0raw: {n * 8} samples a capture at {fs / 1e6:.2f} MS/s ({n * 8 / fs * 1e3:.2f} ms)")
    print("   k   MS/s  ones%   SNR dB (5-30 kHz)   IF kHz   FM noise Hz rms")
    for cap, b in sorted(caps.items()):
        s = bits_of(b)
        print(f"capture {cap}")
        for k in (1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 24, 32):
            snr, fn, fif = [], [], []
            for p in range(min(k, 4)):
                x = s[p::k].astype(float)
                fsk = fs / k
                X = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(len(x)))) ** 2
                f = np.fft.rfftfreq(len(x), 1 / fsk)
                inb = (f >= 5000) & (f <= 30000)
                pk = f[inb][np.argmax(X[inb])]
                sig = inb & (np.abs(f - pk) <= 2500)
                snr.append(10 * np.log10(X[sig].sum() / max(X[inb & ~sig].sum(), 1e-12)))
                z = band(x, fsk, pk - 6000, pk + 6000)
                fi = inst_freq(z, fsk)
                cut = len(fi) // 8
                fi = fi[cut:len(fi) - cut]
                fif.append(fi.mean())
                fn.append(fi.std())
            print(f"  {k:2d} {fs / k / 1e6:6.2f}  {100 * s.mean():4.1f}   {np.mean(snr):8.1f}"
                  f"            {np.mean(fif) / 1e3:7.3f}   {np.median(fn):8.0f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("fm")
    a.add_argument("log")
    a.add_argument("--png")
    a.set_defaults(fn=cmd_fm)
    a = sub.add_parser("rate")
    a.add_argument("log")
    a.set_defaults(fn=cmd_rate)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
