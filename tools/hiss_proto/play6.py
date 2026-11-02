"""Sibilant threshold sweep (sib_db) through the full v3.8 chain: PESQ-NB per clip and the
number of M/edge frames recoded as UU."""
import sys; sys.path.insert(0, '/home/claude/sotto/sim')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb
from scipy.signal import resample_poly
from pesq import pesq
vl.TRACK2['lag'] = 8
clip = sys.argv[1]
x = sf.read(f'/tmp/claude-0/hiss/clips16/{clip}.wav')[0]; x = x[:len(x) // 320 * 320]; x8 = resample_poly(x, 1, 2)
DEC = dict(vowel='speaker', vowel_shape='fine', voiced_trim=-5, blend=True, release_ms=0, keep_mixed=True)
b0 = np.load(f'/tmp/claude-0/hiss/bits/{clip}.npz')['bits']
def sc(b):
    y = vl.synthesise(hb.SottoV38(**DEC).decode(b))[vl.ar.CODEC_DELAY:]; n = min(len(y), len(x8))
    return pesq(8000, x8[:n], y[:n], 'nb')
out = [f"base {sc(b0):.3f}"]
for sd in (0.0, -3.0, -6.0):
    b = hb.SottoV38(vowel='speaker', whisper=True, vsmooth=True, sib_db=sd).encode(x)
    nd = int(((b[:, 0] * 2 + b[:, 1]) != (b0[:, 0] * 2 + b0[:, 1])).sum())
    out.append(f"sib{sd:+.0f} {sc(b):.3f}({nd})")
print(f"{clip:22s} " + '  '.join(out), flush=True)
