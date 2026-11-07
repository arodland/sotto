#!/usr/bin/env python3
"""The K6's speaker sweep, measured: the level of each tone in a recording of the K6's speaker.

  python3 -m modem.k6 mod OUT.wav mod-spksweep.bin ...   (the K6 plays it; record its speaker)
  python3 tools/k6_sweep.py REC.wav [REC2.wav ...] [--json OUT.json] [--plot OUT.png]

The sweep (the RAM module c/k6/mod/spksweep.c) is 1 s of silence, then 14 tones of 1 s at
250 Hz to 7.5 kHz with 0.25 s between, all at the same PWM level, at 16 kHz updates. This finds
the sweep in the recording (the schedule that best fits the tones' energy over time), measures
each tone over the middle 0.6 s of its second (a narrow band around it, so the PWM's images
and the room noise don't count), adds back the PWM hold's sinc loss, and prints each tone in
dB relative to 1 kHz, with its SNR over the same band in the lead silence. The answer for the
hiss band is the 4-8 kHz rows: what the K6 would play there, relative to speech's band.
SPDX-License-Identifier: MIT
"""
import argparse
import json
import sys

import numpy as np

HZ = [250, 400, 630, 1000, 1600, 2000, 2500, 3150, 4000, 5000, 5600, 6300, 7000, 7500]
RATE = 16000                    # the sweep's PWM update rate (spksweep.c RATE)
LEAD, TONE, GAP = 1.0, 1.0, 0.25


def load(path):
    import soundfile as sf
    x, fs = sf.read(path, always_2d=True)
    return x[:, 0].astype(float), fs


def band_db(x, fs, f, bw=0.03):
    """mean power (dB) in f +- bw * f of a windowed stretch"""
    w = np.hanning(len(x))
    s = np.abs(np.fft.rfft(x * w)) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / fs)
    m = (fr > f * (1 - bw)) & (fr < f * (1 + bw))
    return 10 * np.log10(s[m].sum() / (w ** 2).sum() + 1e-30)


def find_start(x, fs):
    """the time of the first tone's onset that best fits the schedule"""
    hop = int(0.01 * fs)
    n = int(0.05 * fs)
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    fr = np.fft.rfftfreq(n, 1 / fs)
    # energy at each tone over time, normalised per tone so loud and quiet tones count alike
    e = np.stack([spec[:, (fr > f * 0.93) & (fr < f * 1.07)].sum(axis=1) for f in HZ])
    e = np.log10(e + 1e-20)
    e = (e - e.mean(axis=1, keepdims=True)) / (e.std(axis=1, keepdims=True) + 1e-9)
    step = int((TONE + GAP) / 0.01)
    on = int(TONE / 0.01)
    gap = step - on
    span = step * len(HZ)
    best, t0 = -np.inf, 0
    # each tone's energy on, minus its own band's in the silence just before it (the lead or
    # the gap): the onsets, to a frame
    for k in range(gap, max(gap + 1, e.shape[1] - span)):
        sc = 0.0
        for j in range(len(HZ)):
            o = k + j * step
            sc += e[j, o + 3:o + on - 3].mean() - e[j, o - gap + 3:o - 3].mean()
        if sc > best:
            best, t0 = sc, k
    return t0 * 0.01 + 0.025


def analyse(path):
    x, fs = load(path)
    t1 = find_start(x, fs)
    lead = x[max(0, int((t1 - LEAD + 0.2) * fs)):int((t1 - 0.2) * fs)]
    rows = []
    for j, f in enumerate(HZ):
        a = t1 + j * (TONE + GAP) + 0.2
        seg = x[int(a * fs):int((a + 0.6) * fs)]
        lv = band_db(seg, fs, f) - 20 * np.log10(np.sinc(f / RATE))
        fl = band_db(lead[:len(seg)], fs, f) if len(lead) >= len(seg) // 2 else float("nan")
        rows.append({"hz": f, "db": lv, "snr_db": lv - fl})
    ref = next(r["db"] for r in rows if r["hz"] == 1000)
    for r in rows:
        r["db"] = round(r["db"] - ref, 1)
        r["snr_db"] = round(r["snr_db"], 1)
    return {"file": path, "fs": fs, "start_s": round(t1, 2), "tones": rows}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav", nargs="+")
    ap.add_argument("--json")
    ap.add_argument("--plot")
    a = ap.parse_args(argv)
    res = [analyse(p) for p in a.wav]
    print("Hz     " + "  ".join(f"{r['file'][-18:]:>18}" for r in res))
    for j, f in enumerate(HZ):
        print(f"{f:5d}  " + "  ".join(f"{r['tones'][j]['db']:+7.1f} dB (SNR {r['tones'][j]['snr_db']:4.0f})" for r in res))
    for r in res:
        print(f"{r['file']}: sweep starts at {r['start_s']} s")
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1)
    if a.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 4))
        for r in res:
            ax.semilogx(HZ, [t["db"] for t in r["tones"]], "o-", label=r["file"].split("/")[-1])
        ax.axvspan(4000, 8000, alpha=0.1)
        ax.set_xlabel("Hz")
        ax.set_ylabel("dB re 1 kHz")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(a.plot, dpi=150)
    return 0


if __name__ == "__main__":
    sys.exit(main())
