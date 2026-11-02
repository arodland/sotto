"""Blind A/B round 16: band-limited hiss on the non-s fricatives (Jeff's idea, 2026-11-02).
  v38:  the v3.8 candidate (as in rounds 14 and 15);
  soft: v3.8 + nons_cap: unvoiced/mixed/edge frames whose decoded 4-8 kHz sits more than
        3 dB under their 2-4 kHz (sh, f, th, z) lose 24 dB/kHz above 5 kHz (at most 40 dB);
  hard: the same, 60 dB/kHz above 4.5 kHz.
s frames and vowels are untouched; decoder only, no bits. Each clip's 8 s window is the one
with the most sh/f/th/z frames. Null pair: v3.8 against itself.
16 kHz clips from /tmp/claude-0/hiss/clips16 (not in the repository).
Usage: python tools/listening/render_ab16.py OUT_AUDIO_DIR   (from the repository root)"""
import sys, os, json, random
import numpy as np, soundfile as sf
sys.path.insert(0, "sim"); sys.path.insert(0, "."); sys.path.insert(0, "tools/hiss_proto/ceiling_study")
import vocoder_lab as vl, nons_cap as nc
from measure import run, hb
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
CLIPS = os.environ.get("CLIPS16", "/tmp/claude-0/hiss/clips16")
vl.TRACK2["lag"] = 8
SEX = {"ears_p020_x": "m", "ears_p047_x": "f", "ears_p051_x": "f", "ears_p095_x": "m"}
CFG = {"soft": (5000.0, 24.0), "hard": (4500.0, 60.0)}
plan = [("v38", "soft", "librispeech_251_m"), ("v38", "soft", "librispeech_8842_f"), ("v38", "soft", "librispeech_3081_f"),
        ("v38", "soft", "librispeech_3536_f"), ("v38", "hard", "librispeech_6313_f"), ("v38", "hard", "librispeech_652_m"),
        ("v38", "hard", "ears_p047_x"), ("v38", "hard", "librispeech_2428_m"), ("v38", "v38", "ears_p051_x")]
rng = random.Random(2026110216); rng.shuffle(plan)
swaps = [True, False] * 5; rng.shuffle(swaps)
def name(c):
    s = "Female" if c.endswith("f") or SEX.get(c) == "f" else "Male"
    return s + (" (EARS studio)" if c.startswith("ears") else f" (LibriSpeech {c.split('_')[1]})")
lvl = lambda y, r: y * np.sqrt(np.mean(r ** 2) / (np.mean(y ** 2) + 1e-12))
w = lambda p, y: sf.write(p, np.round(np.clip(y, -1, 1) * 32767).astype(np.int16), 16000, subtype="PCM_16")
def window(x, b, sec=8.0):
    P = np.abs(hb.stft(x)) ** 2; n = len(P)
    db = lambda a, e: 10 * np.log10(P[:, (hb.FR >= a) & (hb.FR < e)].mean(1) + 1e-12)
    tot = 10 * np.log10(P.sum(1) + 1e-12); lo, mid, hi = db(0, 1000), db(2000, 4000), db(4000, 8000)
    code = b[:, 0] * 2 + b[:, 1]; c = code[np.clip(np.arange(n) + hb.HB_LAG, 0, 2 * len(b) - 1) // 2]
    live = tot > np.percentile(tot, 10) + 10
    nons = live & (((mid > hi + 3) & (mid > lo) & (c == 0)) | ((c != 0) & (c != 1) & (hi > lo - 15)) |
                   ((c == 0) & (np.abs(hi - mid) < 6) & (hi < lo + 6) & (mid > lo - 6)))
    k = int(sec * 100); best = int(np.argmax(np.convolve(nons.astype(float), np.ones(k), "valid"))) if n > k else 0
    return best / 100.0, best / 100.0 + sec, int(nons[best:best + k].sum())
def cut(y, a, e):
    y = y[int(a * 16000):int(e * 16000)].copy(); f = 320
    y[:f] *= np.linspace(0, 1, f); y[-f:] *= np.linspace(1, 0, f); return y
trials, key = [], {}
for n, (p, q, c) in enumerate(plan, 1):
    x, b, y0 = run(f"{CLIPS}/{c}.wav"); y0 = y0[:len(x)]
    Y = {"v38": y0, **{k: nc.cap_nons(y0, b, *v)[0] for k, v in CFG.items()}}
    a0, e0, nf = window(x, b); ref = cut(x, a0, e0)
    A, B = lvl(cut(Y[p], a0, e0), ref), lvl(cut(Y[q], a0, e0), ref)
    if swaps[n - 1]: A, B, p, q = B, A, q, p
    w(f"{OUT}/t{n}a.wav", A); w(f"{OUT}/t{n}b.wav", B); w(f"{OUT}/t{n}o.wav", ref)
    key[n] = dict(test="null" if p == q else "non-s cap", clip=c, A=p, B=q, seconds=[round(a0, 2), round(e0, 2)], nons_frames=nf)
    trials.append(dict(n=n, voice=f"{name(c)}, {a0:.1f}-{e0:.1f} s of the clip"))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key), open(f"{OUT}/../key.json", "w"), indent=1)
