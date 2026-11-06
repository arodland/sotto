#!/usr/bin/env python3
"""Host side of the K6 GP0 modules (c/k6/mod/gp0fm.c, gp0raw.c): parse their `x` dump lines
from a K6 serial log and look at the receive IF they hold (docs/k6-fd6818-audio.md).

  python3 tools/k6_gp0.py fm  LOG [--png out.png]   gp0fm: FM-demodulate each capture
  python3 tools/k6_gp0.py wav OUT.wav                stepped tone sweep for gp0sweep (two passes:
                                                     peak 0.1, then 0.3)
  python3 tools/k6_gp0.py sweep LOG                  gp0sweep: deviation and REG_6F AF level
                                                     per tone, in dB re 1 kHz
  python3 tools/k6_gp0.py rate LOG [--tone 1000]     gp0raw: the IF's quality if the raw
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
                if args.tone:
                    # a capture inside a tone: the residual of a fitted sine plus offset
                    tt = (np.arange(len(fi)) + cut) / fsk
                    A = np.stack([np.sin(2 * np.pi * args.tone * tt),
                                  np.cos(2 * np.pi * args.tone * tt), np.ones_like(tt)], 1)
                    coef, *_ = np.linalg.lstsq(A, fi, rcond=None)
                    fi = fi - A @ coef + coef[2]
                fif.append(fi.mean())
                fn.append(fi.std())
            print(f"  {k:2d} {fs / k / 1e6:6.2f}  {100 * s.mean():4.1f}   {np.mean(snr):8.1f}"
                  f"            {np.mean(fif) / 1e3:7.3f}   {np.median(fn):8.0f}")


SWEEP_HZ = [int(round(150 * (5000 / 150) ** (i / 23))) for i in range(24)]
STEP_S, GAP_S, LEVELS = 0.6, 0.8, (0.1, 0.3)


def cmd_wav(args):
    """0.5 s silence, then per level: the 24 tones of SWEEP_HZ for STEP_S each, GAP_S silence"""
    import wave
    fs = 48000
    parts = [np.zeros(fs // 2)]
    for lv in LEVELS:
        for f in SWEEP_HZ:
            t = np.arange(int(STEP_S * fs)) / fs
            ramp = np.minimum(1, np.minimum(t, t[::-1]) / 0.005)
            parts.append(lv * ramp * np.sin(2 * np.pi * f * t))
        parts.append(np.zeros(int(GAP_S * fs)))
    x = (np.concatenate(parts) * 32767).astype("<i2")
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(x.tobytes())
    print(f"wrote {args.out}: {len(x) / fs:.1f} s, tones {SWEEP_HZ[0]}..{SWEEP_HZ[-1]} Hz,"
          f" levels {LEVELS}")


def cmd_sweep(args):
    rows = []
    for ln in open(args.log, errors="replace"):
        m = re.search(r"(?:^|say )s (\d+) ((?:\w+=\d+ ?)+)\s*$", ln)
        if m:
            rows.append({k: int(v) for k, v in (kv.split("=") for kv in m.group(2).split())})
    if not rows:
        sys.exit("no sweep lines in the log")
    # the floor: the rms of a bare carrier (the quietest fifth of the blocks)
    noise = float(np.percentile([r["rms"] for r in rows], 20))
    # steps: blocks whose tone is within 8% (or 25 Hz) of one SWEEP_HZ value and whose rms is
    # over the floor; a new pass after 8 or more quiet blocks (the gap between passes)
    steps, cur, pas, quiet, started = {}, None, 0, 0, False
    for r in rows:
        if r["rms"] < 1.3 * noise:
            quiet += 1
            cur = None
            continue
        if quiet >= 8 and started:
            pas += 1
        quiet = 0
        f = min(SWEEP_HZ, key=lambda h: abs(h - r["tone"]))
        if abs(f - r["tone"]) > max(0.08 * f, 25):
            cur = None
            continue
        started = True
        if cur != (pas, f):
            cur = (pas, f)
            steps.setdefault(cur, [])
            skip = 2                            # the step's edge; REG_6F moves every ~120 ms
        if skip:
            skip -= 1
            continue
        steps[cur].append(r)
    print(f"{len(rows)} blocks, floor {noise:.0f} Hz rms (a bare carrier)")
    for p in sorted({p for p, _ in steps}):
        ref = steps.get((p, 1000)) or steps.get((p, min(SWEEP_HZ, key=lambda h: abs(h - 1000))))
        def stats(b):
            rms = np.median([r["rms"] for r in b])
            dev = np.sqrt(max(rms ** 2 - noise ** 2, 1.0)) * np.sqrt(2)
            return dev, np.median([r["af"] for r in b])
        d0, a0 = stats(ref) if ref else (1.0, 0.0)
        lv = LEVELS[p] if p < len(LEVELS) else "?"
        print(f"pass {p} (WAV peak {lv}): f Hz, blocks, peak dev Hz, dev dB, AF dB, AF - dev dB"
              " (re 1 kHz)")
        for f in SWEEP_HZ:
            b = steps.get((p, f))
            if not b:
                print(f"  {f:5d}     -")
                continue
            d, a = stats(b)
            dd = 20 * np.log10(d / d0)
            print(f"  {f:5d} {len(b):4d} {d:7.0f} {dd:+6.1f} {a - a0:+6.1f} {a - a0 - dd:+6.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("fm")
    a.add_argument("log")
    a.add_argument("--png")
    a.set_defaults(fn=cmd_fm)
    a = sub.add_parser("wav")
    a.add_argument("out")
    a.set_defaults(fn=cmd_wav)
    a = sub.add_parser("sweep")
    a.add_argument("log")
    a.set_defaults(fn=cmd_sweep)
    a = sub.add_parser("rate")
    a.add_argument("log")
    a.add_argument("--tone", type=float, help="the captures are inside this tone (Hz): FM noise"
                   " is then the residual of a fitted sine")
    a.set_defaults(fn=cmd_rate)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
