"""Blind A/B round 21: sotto 600 / 400 with the 4-8 kHz hiss band (sim/sotto_vlr_hiss.py), 16 kHz.
  6H: sotto 600 hiss (48 bits / 80 ms), Python reference;  6: plain sotto 600 (0-4 kHz);
  4H: sotto 400 hiss (48 bits / 120 ms);                    4: plain sotto 400;
  12H: sotto 1200 hiss (round 19's winner, 48 bits / 40 ms).
One 6H vs 6 pair loses one K6 XL packet in every 8 (superframes with index % 8 == 3).
Usage: python tools/listening/render_ab21.py CLIPS16_DIR OUT_AUDIO_DIR"""
import json, os, random, sys
from multiprocessing import Pool
import numpy as np, soundfile as sf
from scipy.signal import resample_poly

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import vocoder_lab as vl                                   # noqa: E402
import sotto_vlr as vr                                     # noqa: E402
import sotto_vlr_hiss as vh                                # noqa: E402
import sotto_lr_hiss as lh                                 # noqa: E402
from eval_lr_hiss import up, XL_LOST                       # noqa: E402

CLIPS, OUT = sys.argv[1:3]
os.makedirs(OUT, exist_ok=True)
CODES = {"6H": "sotto 600 hiss (16 kHz)", "6": "sotto 600 (0-4 kHz)", "4H": "sotto 400 hiss (16 kHz)",
         "4": "sotto 400 (0-4 kHz)", "12H": "sotto 1200 hiss (16 kHz)"}
LOSS = "loss 1 packet in 8"
plan = [("6H", "6", "librispeech_3081_f", None), ("6H", "6", "ears_p070_f", None),
        ("6H", "6", "librispeech_652_m", None), ("6H", "6", "ears_p107_m", None),
        ("4H", "4", "librispeech_8842_f", None), ("4H", "4", "ears_p020_m", None),
        ("6H", "12H", "ears_p047_f", None), ("6H", "12H", "librispeech_777_m", None),
        ("6H", "6", "librispeech_2428_m", LOSS), ("6H", "6H", "ears_p100_f", None)]
rng = random.Random(2026110621)
rng.shuffle(plan)
swaps = [True, False] * 5
rng.shuffle(swaps)


def render(args):
    code, clip, cond = args
    x = sf.read(f"{CLIPS}/{clip}.wav")[0]
    x = x[:len(x) // 640 * 640]
    n = len(x)
    lost = lambda nb: None if not cond else (np.arange(nb) % 8) != XL_LOST
    if code in ("6H", "4H"):
        c = vh.SottoVLRHiss(code[0] + "00")
        b = c.encode(x)
        return c.decode_audio16(b, lost(len(b)), n16=n)
    if code == "12H":
        c = lh.Sotto1200Hiss()
        b = c.encode(x)
        return c.decode_audio16(b, lost(len(b)), n16=n)
    p = vr.SottoVLR(code + "00")
    b = p.encode(resample_poly(x, 1, 2))
    return up(p.decode_audio(b, lost(len(b)))[vl.ar.CODEC_DELAY:], n)


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

if __name__ == "__main__":
    jobs = sorted({(k, c, cond) for p, q, c, cond in plan for k in {p, q}})
    with Pool(os.cpu_count()) as pool:
        got = dict(zip(jobs, pool.map(render, jobs)))
    trials, key = [], {}
    for n, (p, q, c, cond) in enumerate(plan, 1):
        ref = cut(sf.read(f"{CLIPS}/{c}.wav")[0])
        a, b = (lvl(cut(got[(k, c, cond)]), ref) for k in (p, q))
        if swaps[n - 1]:
            a, b, p, q = b, a, q, p
        w(f"{OUT}/t{n}a.wav", a)
        w(f"{OUT}/t{n}b.wav", b)
        w(f"{OUT}/t{n}o.wav", ref)
        key[n] = dict(test="null" if p == q else "ab", clip=c, A=CODES[p], B=CODES[q], channel=cond or "none")
        trials.append(dict(n=n, voice=f"{name(c)}, " + ("one packet in 8 lost" if cond else "no radio channel")))
        print(n, key[n], flush=True)
    json.dump(dict(trials=trials, key=key, codes=CODES), open(f"{OUT}/../key.json", "w"), indent=1)
