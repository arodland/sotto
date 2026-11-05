"""Blind A/B round 18: sotto 1200 (48 bits / 40 ms, for the UV-K6 pager at FFSK 1800).
  L: sotto 1200 (c/sotto_enc -1200, decoded by the fixed-point decoder the K6 runs);
  V: sotto v3.7 at 2400 bit/s, what the K6 plays today (same encoder and fixed-point decoder);
  C: Codec 2 1200, the open codec at the same rate.
Two pairs lose one K6 XL packet in every 8 (12.5%): with the XL interleave that is every 8th
slot, so v3.7 loses 20 ms every 160 ms and sotto 1200 loses 40 ms every 320 ms (which it
rebuilds from the superframes either side).
Clean held-out voices from /mnt/project-files/vocoder/samples-clean/.
Usage: python tools/listening/render_ab18.py OUT_AUDIO_DIR   (from the repository root; build
c/ with make VARIANT=v3.7 all fixed, and Codec 2 with tools/build_codec2.sh)"""
import sys, os, json, random, subprocess, tempfile
import numpy as np, soundfile as sf
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
C2 = os.path.join(ROOT, "build", "codec2", "src")
CLIPS = "/mnt/project-files/vocoder/samples-clean"
LOSS = "loss 1 packet in 8"
plan = [("L", "C", "librispeech_8842_f", None), ("L", "C", "ears_p095_m", None),
        ("L", "C", "librispeech_3536_f", None), ("L", "C", "librispeech_777_m", None),
        ("L", "V", "ears_p051_f", None), ("L", "V", "librispeech_652_m", None),
        ("L", "V", "ears_p100_f", None), ("L", "V", "librispeech_2428_m", LOSS),
        ("L", "V", "ears_p047_f", LOSS), ("L", "L", "librispeech_6313_f", None)]
rng = random.Random(2026110518); rng.shuffle(plan)
swaps = [True, False] * 5; rng.shuffle(swaps)


def name(c):
    s = "Female" if c.endswith("f") else "Male"
    return s + (" (EARS studio)" if c.startswith("ears") else f" (LibriSpeech {c.split('_')[1]})")


def run(*a):
    subprocess.run([str(x) for x in a], check=True, stdout=subprocess.DEVNULL)


def codec(k, x16, loss):
    with tempfile.TemporaryDirectory() as d:
        x16.tofile(f"{d}/in.raw")
        if k == "C":
            run(f"{C2}/c2enc", "1200", f"{d}/in.raw", f"{d}/b")
            run(f"{C2}/c2dec", "1200", f"{d}/b", f"{d}/o.raw")
            return np.fromfile(f"{d}/o.raw", "<i2") / 32768, 0.0
        flag = ["-1200"] if k == "L" else []
        run(f"{ROOT}/c/sotto_enc", *flag, f"{d}/in.raw", f"{d}/b")
        n = os.path.getsize(f"{d}/b") // 6
        good = np.ones(n, np.uint8)
        if loss:
            good[np.arange(n) % 8 == 3] = 0           # XL packet k = 3 of every group of 8
        good.tofile(f"{d}/g")
        run(f"{ROOT}/c/fixed/sotto_dec_fx", *flag, f"{d}/b", f"{d}/o.raw", f"{d}/g")
        return np.fromfile(f"{d}/o.raw", "<i2") / 32768, float(1 - good.mean())


def align(y, x):
    """Codec 2 1200 has its own delay: shift to the best envelope match (audio_race.search_lag)."""
    sys.path.insert(0, os.path.join(ROOT, "sim"))
    import audio_race as ar
    lag = ar.search_lag(x, y)
    return np.concatenate([y[lag:], np.zeros(lag)])


def cut(y, s=10):
    y = y[:int(s * 8000)].copy(); f = 160
    y[:f] *= np.linspace(0, 1, f); y[-f:] *= np.linspace(1, 0, f); return y


lvl = lambda y, r: y * np.sqrt(np.mean(r ** 2) / (np.mean(y ** 2) + 1e-12))
w = lambda p, y: sf.write(p, np.round(np.clip(y, -1, 1) * 32767).astype(np.int16), 8000, subtype="PCM_16")
trials, key = [], {}
for n, (p, q, c, cond) in enumerate(plan, 1):
    x = sf.read(f"{CLIPS}/{c}.flac")[0]
    x16 = np.round(x * 32767).astype("<i2"); x = x16 / 32768
    ref = cut(x)
    outs, losses = {}, {}
    for k in {p, q}:
        y, losses[k] = codec(k, x16, cond)
        if k == "C":
            y = align(y, x)
        outs[k] = lvl(cut(y), ref)
    a, b = outs[p], outs[q]
    if swaps[n - 1]: a, b, p, q = b, a, q, p
    w(f"{OUT}/t{n}a.wav", a); w(f"{OUT}/t{n}b.wav", b); w(f"{OUT}/t{n}o.wav", ref)
    key[n] = dict(test="null" if p == q else "ab", clip=c, A=p, B=q, channel=cond or "none",
                  slot_loss={k: round(v, 3) for k, v in losses.items()})
    trials.append(dict(n=n, voice=f"{name(c)}, " + ("one packet in 8 lost" if cond else "no radio channel")))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key, codes=dict(L="sotto 1200", V="sotto v3.7 (2400)", C="Codec 2 1200")),
          open(f"{OUT}/../key.json", "w"), indent=1)
