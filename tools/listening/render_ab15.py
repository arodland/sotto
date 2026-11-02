"""Blind A/B round 15: an s model for sotto (tools/hiss_proto/ceiling_study/s_model.py).
  v38:  the v3.8 candidate (as in round 14);
  s14:  v3.8 + the hiss ceiling over the side channel + a 14-bit hiss shape in all-unvoiced
        frames: v3.8's 6-bit 4-band index plus an 8-bit 16-bin (250 Hz) refinement, paid for
        by dropping the last 0-4 kHz shape stage (8 bits) in those frames;
  s14r: s14 with real-noise texture (Rayleigh per-bin magnitudes) in the hiss.
Each clip's 8 s window is the one with the most s frames. Null pair: s14 against itself.
16 kHz clips from /tmp/claude-0/hiss/clips16 (not in the repository).
Usage: python tools/listening/render_ab15.py OUT_AUDIO_DIR   (from the repository root)"""
import sys, os, json, random
import numpy as np, soundfile as sf
sys.path.insert(0, "sim"); sys.path.insert(0, "."); sys.path.insert(0, "tools/hiss_proto/ceiling_study")
import vocoder_lab as vl, ceiling as cl, s_model as sm
from measure import ENC, DEC, run, hb
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
CLIPS = os.environ.get("CLIPS16", "/tmp/claude-0/hiss/clips16")
CACHE = os.environ.get("S14CACHE", "/tmp/claude-0/sib/s14")
vl.TRACK2["lag"] = 8
SEX = {"ears_p020_x": "m", "ears_p047_x": "f", "ears_p051_x": "f", "ears_p095_x": "m"}
plan = [("v38", "s14", "ears_p020_x"), ("v38", "s14", "ears_p095_x"), ("v38", "s14", "librispeech_251_m"),
        ("v38", "s14", "librispeech_6313_f"), ("v38", "s14", "librispeech_8842_f"), ("v38", "s14", "librispeech_3536_f"),
        ("v38", "s14", "librispeech_777_m"), ("s14", "s14r", "librispeech_3081_f"), ("s14", "s14r", "ears_p051_x"),
        ("s14", "s14r", "librispeech_652_m"), ("s14", "s14", "librispeech_2428_m")]
rng = random.Random(2026110215); rng.shuffle(plan)
swaps = [True, False] * 6; rng.shuffle(swaps)
def name(c):
    s = "Female" if c.endswith("f") or SEX.get(c) == "f" else "Male"
    return s + (" (EARS studio)" if c.startswith("ears") else f" (LibriSpeech {c.split('_')[1]})")
lvl = lambda y, r: y * np.sqrt(np.mean(r ** 2) / (np.mean(y ** 2) + 1e-12))
w = lambda p, y: sf.write(p, np.round(np.clip(y, -1, 1) * 32767).astype(np.int16), 16000, subtype="PCM_16")
def outputs(c):
    x, b0, y0 = run(f"{CLIPS}/{c}.wav")
    f = lambda s: f"{CACHE}/{c}_{s}.npy"
    if os.path.exists(f("s14r")):
        b1, y1, y2 = np.load(f("bits")), np.load(f("s14")), np.load(f("s14r"))
    else:
        b1 = sm.SottoS14(**ENC).encode(x)
        y1 = sm.SottoS14(**DEC).decode_audio16(b1, n16=len(x)); y2 = sm.SottoS14(rayleigh=True, **DEC).decode_audio16(b1, n16=len(x))
    return x, b1, {"v38": y0, "s14": y1, "s14r": y2}
def window(x, b, sec=8.0):
    P = np.abs(hb.stft(x)) ** 2; code = b[:, 0] * 2 + b[:, 1]
    fr = np.clip(np.arange(len(P)) + hb.HB_LAG, 0, 2 * len(b) - 1) // 2
    s = (cl.sib_frames(P) & (code[fr] == 0)).astype(float)
    n = int(sec * 100); best = int(np.argmax(np.convolve(s, np.ones(n), "valid"))) if len(s) > n else 0
    return best / 100.0, best / 100.0 + sec
def cut(y, a, e):
    y = y[int(a * 16000):int(e * 16000)].copy(); f = 320
    y[:f] *= np.linspace(0, 1, f); y[-f:] *= np.linspace(1, 0, f); return y
trials, key = [], {}
for n, (p, q, c) in enumerate(plan, 1):
    x, b1, Y = outputs(c)
    a0, e0 = window(x, b1); ref = cut(x, a0, e0)
    A, B = lvl(cut(Y[p], a0, e0), ref), lvl(cut(Y[q], a0, e0), ref)
    if swaps[n - 1]: A, B, p, q = B, A, q, p
    w(f"{OUT}/t{n}a.wav", A); w(f"{OUT}/t{n}b.wav", B); w(f"{OUT}/t{n}o.wav", ref)
    key[n] = dict(test="null" if p == q else ("texture" if {p, q} == {"s14", "s14r"} else "s model"), clip=c, A=p, B=q,
                  seconds=[round(a0, 2), round(e0, 2)])
    trials.append(dict(n=n, voice=f"{name(c)}, {a0:.1f}-{e0:.1f} s of the clip"))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key), open(f"{OUT}/../key.json", "w"), indent=1)
