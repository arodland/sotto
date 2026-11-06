"""Blind A/B round 20: very-low-rate sotto (sim/sotto_vlr.py), 48 bits per 80 or 120 ms.
  6: sotto 600 (48 bits / 80 ms);  4: sotto 400 (48 bits / 120 ms);
  L: sotto 1200 (48 bits / 40 ms);  C7: Codec 2 700C;  C12: Codec 2 1200.
sotto runs the Python references (sim/sotto_vlr.py, sim/sotto_lr.py). One pair loses one K6 XL
packet in every 8 on both sides (through the interleave, superframe 3 of every 8: 80 ms every
640 ms for sotto 600, 40 ms every 320 ms for sotto 1200), rebuilt from the neighbours.
Clean held-out voices from /mnt/project-files/vocoder/samples-clean/.
Usage: python tools/listening/render_ab20.py OUT_AUDIO_DIR   (from the repository root; build
Codec 2 first with tools/build_codec2.sh)"""
import sys, os, json, random, subprocess, tempfile
import numpy as np, soundfile as sf
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sim"))
import audio_race as ar                                        # noqa: E402
import sotto_lr as lr                                          # noqa: E402
import sotto_vlr as vr                                         # noqa: E402
C2 = os.path.join(ROOT, "build", "codec2", "src")
CLIPS = "/mnt/project-files/vocoder/samples-clean"
LOSS = "loss 1 packet in 8"
plan = [("6", "C7", "librispeech_3081_f", None), ("6", "C7", "ears_p107_m", None),
        ("6", "C7", "ears_p070_f", None), ("6", "C12", "librispeech_251_m", None),
        ("6", "C12", "ears_p047_f", None), ("4", "C7", "librispeech_6313_f", None),
        ("4", "C7", "ears_p025_m", None), ("6", "L", "librispeech_777_m", None),
        ("6", "4", "ears_p100_f", None), ("6", "L", "librispeech_2428_m", LOSS),
        ("6", "6", "librispeech_652_m", None)]
rng = random.Random(2026110619); rng.shuffle(plan)
swaps = [True, False] * 6; rng.shuffle(swaps)
NAMES = {"6": "sotto 600", "4": "sotto 400", "L": "sotto 1200", "C7": "Codec 2 700C", "C12": "Codec 2 1200"}


def name(c):
    s = "Female" if c.endswith("f") else "Male"
    return s + (" (EARS studio)" if c.startswith("ears") else f" (LibriSpeech {c.split('_')[1]})")


def codec(k, x, loss):
    if k.startswith("C"):
        mode = {"C7": "700C", "C12": "1200"}[k]
        with tempfile.TemporaryDirectory() as d:
            np.round(x * 32767).astype("<i2").tofile(f"{d}/i.raw")
            subprocess.run([f"{C2}/c2enc", mode, f"{d}/i.raw", f"{d}/b"], check=True)
            subprocess.run([f"{C2}/c2dec", mode, f"{d}/b", f"{d}/o.raw"], check=True)
            y = np.fromfile(f"{d}/o.raw", "<i2") / 32768
        lag = ar.search_lag(x, y)
        return np.concatenate([y[lag:], np.zeros(lag)]), 0.0
    v = lr.Sotto1200() if k == "L" else vr.SottoVLR({"6": "600", "4": "400"}[k])
    bits = v.encode(x)
    good = np.ones(len(bits), bool)
    if loss:
        good[np.arange(len(bits)) % 8 == 3] = False
    y = v.decode_audio(bits, good if loss else None)
    return np.concatenate([y[ar.CODEC_DELAY:], np.zeros(ar.CODEC_DELAY)]), float(1 - good.mean())


def cut(y, s=10):
    y = y[:int(s * 8000)].copy(); f = 160
    y[:f] *= np.linspace(0, 1, f); y[-f:] *= np.linspace(1, 0, f); return y


lvl = lambda y, r: y * np.sqrt(np.mean(r ** 2) / (np.mean(y ** 2) + 1e-12))
w = lambda p, y: sf.write(p, np.round(np.clip(y, -1, 1) * 32767).astype(np.int16), 8000, subtype="PCM_16")
trials, key = [], {}
for n, (p, q, c, cond) in enumerate(plan, 1):
    x = sf.read(f"{CLIPS}/{c}.flac")[0]
    x = np.round(x * 32767) / 32768
    ref = cut(x)
    outs, losses = {}, {}
    for k in {p, q}:
        y, losses[k] = codec(k, x, cond)
        outs[k] = lvl(cut(y), ref)
    a, b = outs[p], outs[q]
    if swaps[n - 1]: a, b, p, q = b, a, q, p
    w(f"{OUT}/t{n}a.wav", a); w(f"{OUT}/t{n}b.wav", b); w(f"{OUT}/t{n}o.wav", ref)
    key[n] = dict(test="null" if p == q else "ab", clip=c, A=NAMES[p], B=NAMES[q], channel=cond or "none",
                  slot_loss={NAMES[k]: round(v, 3) for k, v in losses.items()})
    trials.append(dict(n=n, voice=f"{name(c)}, " + ("one packet in 8 lost" if cond else "no radio channel")))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key), open(f"{OUT}/../key.json", "w"), indent=1)
