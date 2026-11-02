"""Blind A/B round 14: per-speaker hiss ceiling (tools/hiss_proto/ceiling.py) vs v3.8.
Both sides are the v3.8 candidate (encoder vowel="speaker", whisper, vsmooth, z fix; decoder
vowel_shape="fine", blend). The ceiling side also gets (cutoff, steepness) measured from the
whole clip and known to the decoder (no side channel yet), a hiss-index search against the
250 Hz-bin spectrum after the ceiling, and the ceiling gain on the 4-8 kHz layer.
Voices with a steep roll-off: two LibriSpeech readers whose MP3 sources stop at ~7 kHz, and
clips low-passed (10th-order Butterworth, zero phase) at 5-6 kHz to stand in for a dull mic.
The null pair is a normal voice: no ceiling is found, so both sides are the same v3.8.
16 kHz clips from /tmp/claude-0/hiss/clips16 (not in the repository).
Usage: python tools/listening/render_ab14.py OUT_AUDIO_DIR   (from the repository root)"""
import sys, os, json, random
import numpy as np, soundfile as sf
from scipy.signal import butter, sosfiltfilt
sys.path.insert(0, "sim"); sys.path.insert(0, "."); sys.path.insert(0, "tools/hiss_proto/ceiling_study")
import audio_race as ar, vocoder_lab as vl, waveforms as wf, c4fm_sim as cs, ceiling as cl
from modem.link import Transmitter, Receiver
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
CLIPS = os.environ.get("CLIPS16", "/tmp/claude-0/hiss/clips16")
vl.TRACK2["lag"] = 8
ENC = dict(vowel="speaker", whisper=True, vsmooth=True, sib_db=-10.0, sib_voiced=True)
DEC = dict(vowel="speaker", vowel_shape="fine", voiced_trim=-5, blend=True, release_ms=0, keep_mixed=True)
C52 = ("446 MHz, 60 km/h, 52 dB-Hz", 52, cs.doppler(446e6, 60))
plan = [("librispeech_777_m", None, None, (0, 8)), ("librispeech_8842_f", 5000, None, (0, 8)),
        ("librispeech_251_m", 6000, None, (0, 8)), ("librispeech_3170_m", None, None, (0, 8)),
        ("librispeech_652_m", 5000, None, (0, 8)), ("librispeech_3081_f", 5500, None, (0, 8)),
        ("librispeech_8842_f", 5000, C52, (4, 12)), ("librispeech_6313_f", None, None, (0, 8))]
rng = random.Random(2026110214); rng.shuffle(plan)
swaps = [True, False] * 4; rng.shuffle(swaps)
def name(c, lp):
    s = "Female" if c.endswith("f") else "Male"
    s += " (EARS studio)" if c.startswith("ears") else f" (LibriSpeech {c.split('_')[1]})"
    return s + (f", low-passed at {lp/1000:g} kHz" if lp else "")
def cut(y, a, e):
    y = y[int(a * 16000):int(e * 16000)].copy(); f = 320
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
for n, (c, lp, cond, (s0, s1)) in enumerate(plan, 1):
    x = sf.read(f"{CLIPS}/{c}.wav")[0]
    if lp: x = sosfiltfilt(butter(10, lp, fs=16000, output="sos"), x)
    x = x[:len(x) // 320 * 320]; ref = cut(x, s0, s1)
    ceil = cl.measure(x)
    outs, loss = {}, 0.0
    for k, kw in (("v38", {}), ("ceiling", dict(ceiling=ceil))):
        b = cl.SottoV38C(**kw, **ENC).encode(x)
        fr, gd = channel(b, cond, 100 + n) if cond else (b, np.ones(len(b), bool))
        loss = round(float(1 - np.mean(gd)), 3)
        outs[k] = lvl(cut(cl.SottoV38C(**kw, **DEC).decode_audio16(fr, gd, n16=len(x)), s0, s1), ref)
    p, q = "v38", "ceiling" if ceil else "v38"
    a, b = outs[p], outs[q]
    if swaps[n - 1]: a, b, p, q = b, a, q, p
    w(f"{OUT}/t{n}a.wav", a); w(f"{OUT}/t{n}b.wav", b); w(f"{OUT}/t{n}o.wav", ref)
    key[n] = dict(test="null" if p == q else "ab", clip=c, lowpass=lp, ceiling=[int(ceil[0]), int(ceil[1])] if ceil else None,
                  A=p, B=q, channel=cond[0] if cond else "none", frame_loss=loss, seconds=[s0, s1])
    trials.append(dict(n=n, voice=f"{name(c, lp)}, " + (cond[0] if cond else "no radio channel")))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key), open(f"{OUT}/../key.json", "w"), indent=1)
