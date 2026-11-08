"""CW ID workbench: run recorded repeater IDs through the K6's own Morse decoder.

The K6 hears CW only through the FD6818's tone detector (16 tones, 600-1350 Hz, docs/k6-cwid.md),
polled every 2 ms into cw_feed (c/k6/cwid.c, built here for this machine). This tool takes
receive audio (WAV/FLAC, e.g. the IC-7100's FM audio of a local repeater) and decodes it two
ways, so a miss can be put on the detector or on the decoder:

  ideal  keying from a narrow filter on the clip's own CW tone (any frequency): what a perfect
         detector would hand cw_feed. A miss here is the decoder's (timing, speed, call parser).
  k6     a model of the tone detector: 16 Goertzel bins at the K6's 50 Hz steps, a tone counts
         when one bin holds more than --share of the 300-3000 Hz energy, marks stretched
         +10 ms on / +20 ms off as measured on air (docs/k6-air-replay.md). A miss here only is
         the detector's (tone out of range, ID too quiet under voice or courtesy tones).

  python tools/k6_cwbench.py clip.wav [more.wav ...] [--model ideal,k6] [--csv out.csv]
  python tools/k6_cwbench.py --synth "AG7EW/R" --wpm 22 --tone 1000 --snr 10 --voice v.wav -o t.wav

The model's threshold is a guess until it is fitted against clips recorded together with the
K6's own CWID d lines (same ID, same time). SPDX-License-Identifier: MIT"""
import argparse
import csv
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
sys.path.insert(0, ROOT)

NDUR, TEXT = 96, 40          # cwid.h defaults
POLL_MS = 2
TONES = 600 + 50 * np.arange(16)


class CW(ctypes.Structure):
    _fields_ = [("edge_t", ctypes.c_uint32), ("cand_t", ctypes.c_uint32), ("key", ctypes.c_uint8),
                ("cand", ctypes.c_uint8), ("active", ctypes.c_uint8), ("why", ctypes.c_uint8),
                ("n", ctypes.c_uint16), ("nd", ctypes.c_uint16), ("unit", ctypes.c_uint16),
                ("lag", ctypes.c_int16), ("wpm", ctypes.c_uint8), ("tlen", ctypes.c_uint8),
                ("text", ctypes.c_char * TEXT), ("d", ctypes.c_uint8 * NDUR)]


_LIB = None


def lib():
    global _LIB
    if _LIB is None:
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not cc:
            sys.exit("k6_cwbench: needs a C compiler (cc)")
        so = os.path.join(tempfile.mkdtemp(), "libcwid.so")
        subprocess.run([cc, "-O1", "-shared", "-fPIC", os.path.join(K6, "cwid.c"), "-o", so],
                       check=True)
        L = ctypes.CDLL(so)
        L.cw_feed.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
        L.cw_flush.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        L.cw_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint8)]
        _LIB = L
    return _LIB


def load(path):
    import soundfile as sf
    x, fs = sf.read(path, always_2d=True)
    return x.mean(axis=1).astype(np.float64), fs


def frames(x, fs, win_ms):
    """Hann-windowed frames every POLL_MS, each win_ms long, centred on the poll."""
    hop, n = int(round(fs * POLL_MS / 1000)), int(round(fs * win_ms / 1000))
    pad = np.concatenate([np.zeros(n // 2), x, np.zeros(n)])
    m = max(1, (len(x)) // hop)
    idx = np.arange(n)[None, :] + hop * np.arange(m)[:, None]
    return pad[idx] * np.hanning(n)[None, :], n


def dft_power(fr, fs, freqs):
    n = fr.shape[1]
    k = np.exp(-2j * np.pi * np.outer(np.arange(n), np.asarray(freqs, float)) / fs)
    return np.abs(fr @ k) ** 2


def cw_tone(x, fs):
    """The clip's CW tone: the strongest narrow peak in 300-2500 Hz."""
    f = np.fft.rfftfreq(len(x), 1 / fs)
    p = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    band = (f > 300) & (f < 2500)
    return float(f[band][np.argmax(p[band])])


def key_ideal(x, fs, tone=None, win_ms=16.0):
    """Key state per poll from the clip's own tone, as a share of the 300-3000 Hz energy (so the
    open-squelch hiss around a carrier, louder than the ID itself, doesn't key it), threshold
    halfway in dB between the key-up and key-down shares."""
    tone = tone or cw_tone(x, fs)
    fr, n = frames(x, fs, win_ms)
    pt = dft_power(fr, fs, [tone])[:, 0]
    spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    f = np.fft.rfftfreq(n, 1 / fs)
    tot = spec[:, (f >= 300) & (f <= 3000)].sum(axis=1) + 1e-12
    e = 10 * np.log10(pt / tot + 1e-12)
    lo, hi = np.percentile(e, 20), np.percentile(e, 99)
    k = e > (lo + hi) / 2
    return k.astype(int), {"tone": round(tone), "contrast_db": round(hi - lo, 1)}


def key_k6(x, fs, share=0.5, win_ms=12.0, lag_on=10, lag_off=20):
    """The FD6818 tone detector model: tone present when the best of its 16 bins holds more than
    `share` of the 300-3000 Hz energy (both over the same window), with the measured stretch."""
    fr, n = frames(x, fs, win_ms)
    p = dft_power(fr, fs, TONES)
    spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    f = np.fft.rfftfreq(n, 1 / fs)
    tot = spec[:, (f >= 300) & (f <= 3000)].sum(axis=1) + 1e-12   # ~1.5x a pure tone's peak (Hann)
    best = p.max(axis=1)
    on = best / tot > share
    raw = on.copy()
    # the detector says "on" lag_on ms late and lets go lag_off ms late: on at poll i when it
    # was on anywhere in polls i-d_off .. i-d_on
    d_on, d_off = lag_on // POLL_MS, lag_off // POLL_MS
    cs = np.concatenate([[0], np.cumsum(on)])
    i = np.arange(len(on))
    hi, lo = np.clip(i - d_on + 1, 0, len(on)), np.clip(i - d_off, 0, len(on))
    out = cs[hi] - cs[lo] > 0
    hist = np.bincount(p.argmax(axis=1)[raw], minlength=16)
    tone = int(TONES[hist.argmax()]) if raw.any() else 0
    return out.astype(int), {"tone": tone, "on_pct": round(100 * raw.mean(), 1)}


def decode(keys):
    """Feed cw_feed one poll per POLL_MS; every transmission's (text, wpm, call)."""
    L = lib()
    c = CW()
    L.cw_init(ctypes.byref(c))
    out, t = [], 1000
    for k in list(keys) + [0] * (3000 // POLL_MS):
        if L.cw_feed(ctypes.byref(c), t, int(k)) and c.nd:
            text = c.text.decode(errors="replace")
            buf, fl = ctypes.create_string_buffer(11), ctypes.c_uint8()
            call = buf.value.decode() if L.cw_call(c.text, buf, ctypes.byref(fl)) else ""
            out.append({"text": text, "wpm": c.wpm, "lag": c.lag, "call": call})
        t += POLL_MS
    return out


def synth(a):
    from modem import k6
    fs = 48000
    sig = k6.cw(a.synth, fs=fs, wpm=a.wpm, tone=a.tone, level=1.0)
    sig = np.concatenate([np.zeros(fs), sig, np.zeros(fs)])
    rnd = np.random.default_rng(a.seed)
    out = sig * 10 ** (a.cw_db / 20)
    if a.voice:
        v, vfs = load(a.voice)
        if vfs != fs:
            v = np.interp(np.arange(0, len(v), vfs / fs), np.arange(len(v)), v)
        v = np.resize(v, len(out))
        v = v / (np.sqrt(np.mean(v ** 2)) + 1e-12) * np.sqrt(np.mean(sig[sig != 0] ** 2))
        out = out + v
    if a.snr is not None:
        p = np.mean((sig[sig != 0] * 10 ** (a.cw_db / 20)) ** 2)
        out = out + rnd.normal(0, np.sqrt(p / 10 ** (a.snr / 10)), len(out))
    import soundfile as sf
    sf.write(a.o, 0.5 * out / max(1e-9, np.abs(out).max()), fs)
    print("wrote", a.o)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("clips", nargs="*")
    ap.add_argument("--model", default="ideal,k6")
    ap.add_argument("--share", type=float, default=0.5, help="k6 model: tone share of the band")
    ap.add_argument("--td-lo", type=int, default=600, help="k6 model: the tone bank's lowest tone, Hz")
    ap.add_argument("--td-step", type=int, default=50, help="k6 model: the bank's spacing (16 tones), Hz")
    ap.add_argument("--csv")
    ap.add_argument("--synth", help="make a test clip of this text instead")
    ap.add_argument("--wpm", type=float, default=20)
    ap.add_argument("--tone", type=float, default=800)
    ap.add_argument("--cw-db", type=float, default=0, help="CW level against the voice, dB")
    ap.add_argument("--snr", type=float, help="white noise, dB below the CW")
    ap.add_argument("--voice", help="mix this audio under the CW")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-o", default="cw-test.wav")
    a = ap.parse_args()
    global TONES
    TONES = a.td_lo + a.td_step * np.arange(16)
    if a.synth:
        return synth(a)
    rows = []
    for path in a.clips:
        x, fs = load(path)
        for m in a.model.split(","):
            keys, info = key_ideal(x, fs) if m == "ideal" else key_k6(x, fs, a.share)
            msgs = decode(keys)
            calls = [d["call"] for d in msgs if d["call"]]
            best = max(msgs, key=lambda d: len(d["text"]), default={"text": "", "wpm": 0, "call": ""})
            row = {"clip": os.path.basename(path), "model": m, **info, "msgs": len(msgs),
                   "calls": " ".join(calls), "wpm": best["wpm"], "text": best["text"]}
            rows.append(row)
            print(f"{row['clip']:40.40} {m:5} tone={info['tone']:>4} msgs={len(msgs):2} "
                  f"wpm={best['wpm']:2} call={row['calls'] or '-':12} {best['text']!r}")
    if a.csv and rows:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
            w.writeheader()
            w.writerows(rows)


if __name__ == "__main__":
    main()
