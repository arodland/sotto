"""Labelled side-by-side page: original / v3.8 / v3.9 candidate (sim/hiss_v39.py).
Not blind: every row names its versions. Rows:
  clean   whole clips, eleven voices; v3.9 sends v3.8's exact bits, only the non-s cap differs
  lowpass clips low-passed (10th-order Butterworth, zero phase) at 5-5.5 kHz, a dull mic
  cut     the two LibriSpeech voices whose source MP3s stop near 7 kHz (777, 3170)
  fade    v3.8 bits over a fading FM channel (2 m and 70 cm, 60 km/h), same frames for both
Each output is level-matched to its original. WB- and NB-PESQ go in the key.
16 kHz clips from /tmp/claude-0/hiss/clips16 (not in the repository).
Usage: python tools/listening/render_v39_compare.py OUT_AUDIO_DIR   (from the repository root)"""
import sys, os, json
import numpy as np, soundfile as sf
from scipy.signal import butter, sosfiltfilt, resample_poly
from pesq import pesq
sys.path.insert(0, "sim"); sys.path.insert(0, "."); sys.path.insert(0, "tools/hiss_proto/ceiling_study")
import audio_race as ar, vocoder_lab as vl, waveforms as wf, c4fm_sim as cs
from modem.link import Transmitter, Receiver
from measure import hb, ENC, DEC
import hiss_v39 as h9
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
CLIPS = os.environ.get("CLIPS16", "/tmp/claude-0/hiss/clips16")
vl.TRACK2["lag"] = 8
SEX = {"ears_p020_x": "m", "ears_p047_x": "f", "ears_p051_x": "f", "ears_p095_x": "m"}
C2M = ("146 MHz, 60 km/h, 50 dB-Hz", 50, cs.doppler(146e6, 60))
C70 = ("446 MHz, 60 km/h, 52 dB-Hz", 52, cs.doppler(446e6, 60))
plan = [("clean", c, None) for c in ["librispeech_8842_f", "librispeech_6313_f", "librispeech_3536_f",
                                      "librispeech_3081_f", "ears_p047_x", "ears_p051_x", "librispeech_652_m",
                                      "librispeech_251_m", "librispeech_2428_m", "ears_p020_x", "ears_p095_x"]]
plan += [("lowpass", "librispeech_8842_f", 5500), ("lowpass", "librispeech_251_m", 5000),
         ("cut", "librispeech_777_m", None), ("cut", "librispeech_3170_m", None),
         ("fade", "librispeech_6313_f", C2M), ("fade", "librispeech_652_m", C70)]


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


def scores(x, y):
    y = y[:len(x)]
    return (round(float(pesq(16000, x, y, "wb")), 3),
            round(float(pesq(8000, resample_poly(x, 1, 2), resample_poly(y, 1, 2), "nb")), 3))


trials, key = [], {}
for n, (kind, c, arg) in enumerate(plan, 1):
    x = sf.read(f"{CLIPS}/{c}.wav")[0]; x = x[:len(x) // 320 * 320]
    if kind == "lowpass":
        x = sosfiltfilt(butter(10, arg, fs=16000, output="sos"), x)
    b8 = hb.SottoV38(**ENC).encode(x)
    b9 = h9.SottoV39(**ENC).encode(x)
    good, loss = None, 0.0
    if kind == "fade":
        assert np.array_equal(b8, b9)
        b8, good = channel(b8, arg, 100 + n); b9 = b8; loss = round(float(1 - good.mean()), 3)
    y8 = hb.SottoV38(**DEC).decode_audio16(b8, good, n16=len(x))[:len(x)]
    d9 = h9.SottoV39(**DEC)
    y9 = d9.decode_audio16(b9, good, n16=len(x))[:len(x)]
    ce = d9.frame_ceiling(b9, good)
    g0 = next((g for g, v in enumerate(ce) if v is not None), None)
    y8, y9 = lvl(y8, x), lvl(y9, x)
    w(f"{OUT}/t{n}o.wav", x); w(f"{OUT}/t{n}a.wav", y8); w(f"{OUT}/t{n}b.wav", y9)
    s8, s9 = scores(x, y8), scores(x, y9)
    key[n] = dict(kind=kind, clip=c, a="v3.8", b="v3.9", lowpass=arg if kind == "lowpass" else None,
                  channel=arg[0] if kind == "fade" else "none", frame_loss=loss,
                  same_bits=bool(np.array_equal(b8, b9)), ceiling=list(ce[g0]) if g0 is not None else None,
                  ceiling_from_s=round(g0 * 0.02, 2) if g0 is not None else None,
                  wb_pesq=[s8[0], s9[0]], nb_pesq=[s8[1], s9[1]])
    if kind == "clean":
        note = "same bits; only the non-s cap differs"
    elif kind == "lowpass":
        note = f"low-passed at {arg / 1000:g} kHz"
    elif kind == "cut":
        note = "source MP3 stops near 7 kHz"
    else:
        note = f"{arg[0]}, {100 * loss:.0f}% frames lost"
    if g0 is not None:
        note += f"; v3.9 ceiling {ce[g0][0] / 1000:g} kHz from {g0 * 0.02:.1f} s"
    trials.append(dict(n=n, voice=name(c), note=note, kind=kind))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key), open(f"{OUT}/../key.json", "w"), indent=1)
