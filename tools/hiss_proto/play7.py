"""The UU pitch-field leak: with v3.8 bits (hiss index in UU frames' Wo field) an edge frame's
mixed second half took its Wo from the following UU frame. Fresh v3.8 encode per clip, PESQ-NB
with the guard off and on, and the number of 10 ms subframes the guard changes."""
import sys; sys.path.insert(0, '/home/claude/sotto/sim')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb
from scipy.signal import resample_poly
from pesq import pesq
vl.TRACK2['lag'] = 8
clip = sys.argv[1]
x = sf.read(f'/tmp/claude-0/hiss/clips16/{clip}.wav')[0]; x = x[:len(x) // 320 * 320]; x8 = resample_poly(x, 1, 2)
DEC = dict(vowel='speaker', vowel_shape='fine', voiced_trim=-5, blend=True, release_ms=0, keep_mixed=True)
b = hb.SottoV38(vowel='speaker', whisper=True, vsmooth=True, sib_db=3.0).encode(x)
np.save(f'/tmp/claude-0/hiss/bits/{clip}_full.npy', b)
out = []
ms = []
for guard in (False, True):
    d = hb.SottoV38(**DEC); d.UU_WO_GUARD = guard
    m = d.decode(b); ms.append(m)
    y = vl.synthesise(m)[vl.ar.CODEC_DELAY:]; n = min(len(y), len(x8))
    out.append(f"{'guard' if guard else 'leak '} {pesq(8000, x8[:n], y[:n], 'nb'):.3f}")
nd = int((ms[0]['Wo'] != ms[1]['Wo']).sum())
print(f"{clip:22s} " + '  '.join(out) + f"  subframes changed {nd}", flush=True)
