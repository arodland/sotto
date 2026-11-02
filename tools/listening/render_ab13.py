"""Blind A/B round 13: high band in vowels for sotto v3.8. All three are the round 12
winner (burst rule, mixed-frame hiss, gated boost, onset recoding) as SottoV38:
  C: no high band in voiced frames;
  G: voiced frames get the decoder's guess - 3 dB (no bits);
  A: voiced and mixed frames at the guess + a per-speaker offset sent 1 bit per UU frame
     (16-bit message per 2 s; UU index 6 bits). Offsets start from the default and update
     from the second window, so the first ~4 s of a clip uses the default.
16 kHz clips (16 kHz originals in /tmp/claude-0/hiss/clips16, not in the repository).
Usage: python tools/listening/render_ab13.py OUT_AUDIO_DIR   (from the repository root)"""
import sys, os, json, random
import numpy as np, soundfile as sf
sys.path.insert(0, "sim"); sys.path.insert(0, ".")
import audio_race as ar, vocoder_lab as vl, hiss_band as hb, waveforms as wf, c4fm_sim as cs
from modem.link import Transmitter, Receiver
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
CLIPS = os.environ.get("CLIPS16", "/tmp/claude-0/hiss/clips16")
vl.TRACK2["lag"] = 8
C52 = ("446 MHz, 60 km/h, 52 dB-Hz", 52, cs.doppler(446e6, 60))
V30 = ("146 MHz, 30 km/h, 56 dB-Hz", 56, cs.doppler(146e6, 30))
VAR = {"C": None, "G": "guess", "A": "adaptive"}
plan = [("C", "A", "ears_p047_f", None), ("C", "G", "ears_p020_m", None), ("G", "A", "ears_p051_f", None),
        ("C", "A", "librispeech_8842_f", C52), ("C", "A", "ears_p095_m", None), ("G", "A", "ears_p047_f", V30),
        ("C", "G", "librispeech_652_m", None), ("C", "A", "librispeech_3536_f", None),
        ("G", "A", "librispeech_2428_m", C52), ("A", "A", "ears_p020_m", None)]
rng = random.Random(2026110113); rng.shuffle(plan)
swaps = [True, False] * 5; rng.shuffle(swaps)
def name(c):
    s = "Female" if c.endswith("f") else "Male"
    return s + (" (EARS studio)" if c.startswith("ears") else f" (LibriSpeech {c.split('_')[1]})")
def cut(y, s=10):
    y = y[:int(s * 16000)].copy(); f = 320
    y[:f] *= np.linspace(0, 1, f); y[-f:] *= np.linspace(1, 0, f); return y
lvl = lambda y, r: y * np.sqrt(np.mean(r ** 2) / (np.mean(y ** 2) + 1e-12))
w = lambda p, y: sf.write(p, np.round(np.clip(y, -1, 1) * 32767).astype(np.int16), 16000, subtype="PCM_16")
def channel(bits, cond, seed):
    mute = np.zeros(48, np.int8); n = len(bits)
    tx = Transmitter("sotto_vw").modulate(bits)
    disc, _ = wf.fm_channel(tx, cond[1], fd=cond[2], seed=seed)
    rx = Receiver("sotto_vw", mute=mute).receive(disc, t0=ar.LEAD + 4800)
    fr = np.concatenate([rx["frames"], np.repeat(mute[None], max(0, n - len(rx["frames"])), 0)])[:n]
    good = np.concatenate([rx["good"], np.zeros(max(0, n - len(rx["good"])), bool)])[:n]
    return fr, good
trials, key = [], {}
for n, (p, q, c, cond) in enumerate(plan, 1):
    x = sf.read(f"{CLIPS}/{c}.wav")[0]; x = x[:len(x) // 320 * 320]; ref = cut(x)
    loss = 0.0
    def dec(k):
        global loss
        s = hb.SottoV38(vowel=VAR[k]); b = s.encode(x)
        fr, gd = channel(b, cond, 100 + n) if cond else (b, np.ones(len(b), bool))
        loss = round(float(1 - np.mean(gd)), 3)
        return s.decode_audio16(fr, gd, n16=len(x))
    a, b = lvl(cut(dec(p)), ref), lvl(cut(dec(q)), ref)
    if swaps[n - 1]: a, b, p, q = b, a, q, p
    w(f"{OUT}/t{n}a.wav", a); w(f"{OUT}/t{n}b.wav", b); w(f"{OUT}/t{n}o.wav", ref)
    key[n] = dict(test="null" if p == q else "ab", clip=c, A=p, B=q, channel=cond[0] if cond else "none", frame_loss=loss)
    trials.append(dict(n=n, voice=f"{name(c)}, " + (cond[0] if cond else "no radio channel")))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key), open(f"{OUT}/../key.json", "w"), indent=1)
