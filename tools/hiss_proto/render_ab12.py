"""Blind A/B round 12: t/p bursts. hiss7 (round 11 winner) vs hiss7b (encoder sends burst frames as UU so they get a hiss index) vs hiss7bm (hiss7b + decoder-guessed hiss in mixed frames).
Round 11 docstring follows.
Blind A/B round 11: unvoiced consonants. v3.6 vs 'level' (unvoiced noise at 50 Hz spacing,
+3 dB) vs 'hiss7' (level + 4-8 kHz hiss, 7-bit index in all-unvoiced frames) vs 'hiss0'
(decoder guess). 16 kHz WAV, original 16 kHz, clean and fading channels."""
import sys, json, random
sys.path.insert(0, '/home/claude/sotto'); sys.path.insert(0, '/home/claude/sotto/sim')
from hiss import *
import audio_race as ar, waveforms as wf, c4fm_sim as cs
from modem.link import Transmitter, Receiver
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
C52 = ('446 MHz, 60 km/h, 52 dB-Hz', 52, cs.doppler(446e6, 60))
C56 = ('446 MHz, 60 km/h, 56 dB-Hz', 56, cs.doppler(446e6, 60))
V30 = ('146 MHz, 30 km/h, 56 dB-Hz', 56, cs.doppler(146e6, 30))
plan = [('hiss7', 'hiss7bm', 'ears_p020_m', None), ('hiss7', 'hiss7bm', 'ears_p095_m', None),
        ('hiss7', 'hiss7b', 'librispeech_652_m', None), ('hiss7', 'hiss7bm', 'librispeech_8842_f', C52),
        ('hiss7b', 'hiss7bm', 'ears_p051_f', None), ('hiss7', 'hiss7bm', 'ears_p047_f', V30),
        ('hiss7b', 'hiss7bm', 'librispeech_3536_f', None), ('hiss7', 'hiss7b', 'librispeech_2428_m', C52),
        ('hiss7bm', 'hiss7bm', 'ears_p095_m', None)]
VAR = {'hiss7': (0, None), 'hiss7b': (12, None), 'hiss7bm': (12, -3.0)}
rng = random.Random(2026110112); rng.shuffle(plan)
swaps = [True, False] * 6; rng.shuffle(swaps)
plan.append(('hiss7', 'hiss7bm', 'ears_p047_f', None))   # Jeff: keep tabs on round 11 pair 8 (p047)
h = Hiss('hiss_model.npz'); v37 = vl.SottoV37()
def name(c):
    s = 'Female' if c.endswith('f') else 'Male'
    return s + (' (EARS studio)' if c.startswith('ears') else f" (LibriSpeech {c.split('_')[1]})")
def cut(y, s=10):
    y = y[:int(s * 16000)].copy(); f = 320
    y[:f] *= np.linspace(0, 1, f); y[-f:] *= np.linspace(1, 0, f); return y
lvl = lambda y, r: y * np.sqrt(np.mean(r ** 2) / (np.mean(y ** 2) + 1e-12))
w = lambda p, y: sf.write(p, np.round(np.clip(y, -1, 1) * 32767).astype(np.int16), 16000, subtype='PCM_16')
def channel(bits, cond, seed):
    mute = np.zeros(48, np.int8); n = len(bits)
    tx = Transmitter('sotto_vw').modulate(bits)
    disc, _ = wf.fm_channel(tx, cond[1], fd=cond[2], seed=seed)
    rx = Receiver('sotto_vw', mute=mute).receive(disc, t0=ar.LEAD + 4800)
    fr = np.concatenate([rx['frames'], np.repeat(mute[None], max(0, n - len(rx['frames'])), 0)])[:n]
    good = np.concatenate([rx['good'], np.zeros(max(0, n - len(rx['good'])), bool)])[:n]
    return fr, good
trials, key = [], {}
for n, (p, q, c, cond) in enumerate(plan, 1):
    x16 = sf.read(f'clips16/{c}.wav')[0]; x16 = x16[:len(x16) // 320 * 320]; ref = cut(x16)
    def dec(k):
        br, mh = VAR[k]; hh = Hiss('hiss_model.npz', burst=br); bb = hh.encode(x16)
        if cond: fr, gd = channel(bb, cond, 100 + n)
        else: fr, gd = bb, np.ones(len(bb), bool)
        dec.loss = round(float(1 - np.mean(gd)), 3)
        return hh.decode(fr, gd, mode='hiss7', n16=len(x16), mixed_hb=mh)
    a, b = lvl(cut(dec(p)), ref), lvl(cut(dec(q)), ref)
    loss = dec.loss
    if swaps[n - 1]: a, b, p, q = b, a, q, p
    w(f'{OUT}/t{n}a.wav', a); w(f'{OUT}/t{n}b.wav', b); w(f'{OUT}/t{n}o.wav', ref)
    key[n] = dict(test='null' if p == q else 'ab', clip=c, A=p, B=q, channel=cond[0] if cond else 'none', frame_loss=loss)
    trials.append(dict(n=n, voice=f"{name(c)}, " + (f'{cond[0]}' if cond else 'no radio channel')))
    print(n, key[n], flush=True)
json.dump(dict(trials=trials, key=key), open(f'{OUT}/../key.json', 'w'), indent=1)
