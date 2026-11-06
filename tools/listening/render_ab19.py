"""Blind A/B round 19: sotto 1200 with the 4-8 kHz hiss band (sim/sotto_lr_hiss.py), 16 kHz.
  H: sotto 1200 hiss (48 bits / 40 ms; B frames 2-bit hiss + side bit), Python reference;
  L: sotto 1200 as the K6 plays it today (0-4 kHz only), Python reference;
  8: sotto v3.8 at 2400 bit/s (same hiss band, twice the rate).
One H vs L pair loses one K6 XL packet in every 8 (superframes with index % 8 == 3).
The clips are tools/eval_lr_hiss.py --wav renders of the 16 held-out voices at 16 kHz.
Usage: python tools/listening/render_ab19.py EVAL_WAV_DIR CLIPS16_DIR OUT_AUDIO_DIR"""
import json, os, random, sys
import numpy as np, soundfile as sf

WAV, CLIPS, OUT = sys.argv[1:4]
os.makedirs(OUT, exist_ok=True)
FILE = dict(H="lr_hiss_side", L="lr", **{"8": "v38"})
CODES = dict(H="sotto 1200 hiss (16 kHz)", L="sotto 1200 (0-4 kHz, as on the K6)", **{"8": "sotto v3.8 2400 (16 kHz)"})
LOSS = "loss 1 packet in 8"
plan = [("H", "L", "librispeech_8842_f", None), ("H", "L", "ears_p095_m", None),
        ("H", "L", "ears_p051_f", None), ("H", "L", "librispeech_251_m", None),
        ("H", "L", "ears_p100_f", None), ("H", "L", "librispeech_2428_m", LOSS),
        ("H", "8", "ears_p020_m", None), ("H", "8", "librispeech_3536_f", None),
        ("H", "8", "librispeech_777_m", None), ("H", "H", "librispeech_6313_f", None)]
rng = random.Random(2026110619)
rng.shuffle(plan)
swaps = [True, False] * 5
rng.shuffle(swaps)


def name(c):
    s = "Female" if c.endswith("f") else "Male"
    return s + (" (EARS studio)" if c.startswith("ears") else f" (LibriSpeech {c.split('_')[1]})")


def cut(y, s=10):
    y = y[:int(s * 16000)].copy()
    f = 320
    y[:f] *= np.linspace(0, 1, f)
    y[-f:] *= np.linspace(1, 0, f)
    return y


lvl = lambda y, r: y * np.sqrt(np.mean(r ** 2) / (np.mean(y ** 2) + 1e-12))
w = lambda p, y: sf.write(p, np.round(np.clip(y, -1, 1) * 32767).astype(np.int16), 16000, subtype="PCM_16")
trials, key = [], {}
for n, (p, q, c, cond) in enumerate(plan, 1):
    x = sf.read(f"{CLIPS}/{c}.wav")[0]
    ref = cut(x)
    sfx = "_loss" if cond else ""
    outs = {k: lvl(cut(sf.read(f"{WAV}/{c}__{FILE[k]}{sfx}.wav")[0]), ref) for k in {p, q}}
    a, b = outs[p], outs[q]
    if swaps[n - 1]:
        a, b, p, q = b, a, q, p
    w(f"{OUT}/t{n}a.wav", a)
    w(f"{OUT}/t{n}b.wav", b)
    w(f"{OUT}/t{n}o.wav", ref)
    key[n] = dict(test="null" if p == q else "ab", clip=c, A=p, B=q, channel=cond or "none")
    trials.append(dict(n=n, voice=f"{name(c)}, " + ("one packet in 8 lost" if cond else "no radio channel")))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key, codes=CODES), open(f"{OUT}/../key.json", "w"), indent=1)
