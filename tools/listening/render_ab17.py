"""Blind A/B round 17: settling the two hiss layers (docs/hiss-ceiling.md).
  v38:   the v3.8 candidate;  soft / hard: v3.8 + the non-s cap (nons_cap.py, 5 kHz 24 dB/kHz
         or 4.5 kHz 60 dB/kHz);  softz: soft, but frames with a voicing bar (decoded 0-1 kHz
         above 2-4 kHz: z, v, zh) are left alone;
  ceil:  v3.8 + the hiss ceiling measured from the whole clip (round 14);  ceilsoft: ceil + soft.
Comparisons: soft vs hard (4), soft vs softz (3, windows with the most voiced fricatives),
ceil vs ceilsoft on the two MP3-cut voices (2), v38 vs soft over 446 MHz fading (1), null (1).
16 kHz clips from /tmp/claude-0/hiss/clips16 (not in the repository).
Usage: python tools/listening/render_ab17.py OUT_AUDIO_DIR   (from the repository root)"""
import sys, os, json, random
import numpy as np, soundfile as sf
sys.path.insert(0, "sim"); sys.path.insert(0, "."); sys.path.insert(0, "tools/hiss_proto/ceiling_study")
import audio_race as ar, vocoder_lab as vl, waveforms as wf, c4fm_sim as cs, ceiling as cl, nons_cap as nc
from modem.link import Transmitter, Receiver
from measure import run, hb, ENC, DEC
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
CLIPS = os.environ.get("CLIPS16", "/tmp/claude-0/hiss/clips16")
vl.TRACK2["lag"] = 8
SEX = {"ears_p020_x": "m", "ears_p047_x": "f", "ears_p051_x": "f", "ears_p095_x": "m"}
SOFT, HARD = dict(fc=5000.0, S=24.0), dict(fc=4500.0, S=60.0)
C52 = ("446 MHz, 60 km/h, 52 dB-Hz", 52, cs.doppler(446e6, 60))
plan = [("soft", "hard", "librispeech_8842_f", "nons", None), ("soft", "hard", "librispeech_251_m", "nons", None),
        ("soft", "hard", "librispeech_6313_f", "nons", None), ("soft", "hard", "librispeech_652_m", "nons", None),
        ("soft", "softz", "librispeech_3536_f", "z", None), ("soft", "softz", "librispeech_2428_m", "z", None),
        ("soft", "softz", "librispeech_3081_f", "z", None),
        ("ceil", "ceilsoft", "librispeech_777_m", "nons", None), ("ceil", "ceilsoft", "librispeech_3170_m", "nons", None),
        ("v38", "soft", "librispeech_8842_f", "fade", C52), ("soft", "soft", "ears_p095_x", "nons", None)]
rng = random.Random(2026110217); rng.shuffle(plan)
swaps = [True, False] * 6; rng.shuffle(swaps)
def name(c):
    s = "Female" if c.endswith("f") or SEX.get(c) == "f" else "Male"
    return s + (" (EARS studio)" if c.startswith("ears") else f" (LibriSpeech {c.split('_')[1]})")
lvl = lambda y, r: y * np.sqrt(np.mean(r ** 2) / (np.mean(y ** 2) + 1e-12))
w = lambda p, y: sf.write(p, np.round(np.clip(y, -1, 1) * 32767).astype(np.int16), 16000, subtype="PCM_16")
def channel(bits, cond, seed):
    mute = np.zeros(48, np.int8); n = len(bits)
    disc, _ = wf.fm_channel(Transmitter("sotto_vw").modulate(bits), cond[1], fd=cond[2], seed=seed)
    rx = Receiver("sotto_vw", mute=mute).receive(disc, t0=ar.LEAD + 4800)
    fr = np.concatenate([rx["frames"], np.repeat(mute[None], max(0, n - len(rx["frames"])), 0)])[:n]
    return fr, np.concatenate([rx["good"], np.zeros(max(0, n - len(rx["good"])), bool)])[:n]
def window(x, b, kind, sec=8.0):
    if kind == "fade": return 4.0, 12.0
    P = np.abs(hb.stft(x)) ** 2; n = len(P)
    db = lambda a, e: 10 * np.log10(P[:, (hb.FR >= a) & (hb.FR < e)].mean(1) + 1e-12)
    tot = 10 * np.log10(P.sum(1) + 1e-12); lo, mid, hi = db(0, 1000), db(2000, 4000), db(4000, 8000)
    c = (b[:, 0] * 2 + b[:, 1])[np.clip(np.arange(n) + hb.HB_LAG, 0, 2 * len(b) - 1) // 2]
    live = tot > np.percentile(tot, 10) + 10
    if kind == "z": sel = live & (c != 1) & (hi > lo - 15) & (lo > mid)
    else: sel = live & (c != 1) & (hi < mid - 3)
    k = int(sec * 100); best = int(np.argmax(np.convolve(sel.astype(float), np.ones(k), "valid"))) if n > k else 0
    return best / 100.0, best / 100.0 + sec
def cut(y, a, e):
    y = y[int(a * 16000):int(e * 16000)].copy(); f = 320
    y[:f] *= np.linspace(0, 1, f); y[-f:] *= np.linspace(1, 0, f); return y
trials, key = [], {}
for n, (p, q, c, kind, cond) in enumerate(plan, 1):
    x, b, y0 = run(f"{CLIPS}/{c}.wav"); y0 = y0[:len(x)]; loss = 0.0
    if cond:
        fr, gd = channel(b, cond, 100 + n); loss = round(float(1 - gd.mean()), 3)
        y0 = hb.SottoV38(**DEC).decode_audio16(fr, gd, n16=len(x)); bb = fr
    else:
        bb = b
    Y = {"v38": y0, "soft": nc.cap_nons(y0, bb, **SOFT)[0]}
    if "hard" in (p, q): Y["hard"] = nc.cap_nons(y0, bb, **HARD)[0]
    if "softz" in (p, q): Y["softz"] = nc.cap_nons(y0, bb, z_exempt=0.0, **SOFT)[0]
    if "ceil" in (p, q):
        cc = cl.measure(x); bc = cl.SottoV38C(ceiling=cc, **ENC).encode(x)
        Y["ceil"] = cl.SottoV38C(ceiling=cc, **DEC).decode_audio16(bc, n16=len(x)); Y["ceilsoft"] = nc.cap_nons(Y["ceil"], bc, **SOFT)[0]
    a0, e0 = window(x, b, kind); ref = cut(x, a0, e0)
    A, B = lvl(cut(Y[p], a0, e0), ref), lvl(cut(Y[q], a0, e0), ref)
    if swaps[n - 1]: A, B, p, q = B, A, q, p
    w(f"{OUT}/t{n}a.wav", A); w(f"{OUT}/t{n}b.wav", B); w(f"{OUT}/t{n}o.wav", ref)
    key[n] = dict(test="null" if p == q else kind, clip=c, A=p, B=q, seconds=[round(a0, 2), round(e0, 2)],
                  channel=cond[0] if cond else "none", frame_loss=loss)
    trials.append(dict(n=n, voice=f"{name(c)}, {a0:.1f}-{e0:.1f} s, " + (cond[0] if cond else "no radio channel")))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key), open(f"{OUT}/../key.json", "w"), indent=1)
