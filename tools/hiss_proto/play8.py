"""Voiced-fricative recode (sib_voiced, sib_db -10) vs the v3.8 encoder as proposed: fresh
encodes, PESQ-NB, frames recoded."""
import sys; sys.path.insert(0, '/home/claude/sotto/sim')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb
from scipy.signal import resample_poly
from pesq import pesq
vl.TRACK2['lag'] = 8
clip = sys.argv[1]
x = sf.read(f'/tmp/claude-0/hiss/clips16/{clip}.wav')[0]; x = x[:len(x) // 320 * 320]; x8 = resample_poly(x, 1, 2)
DEC = dict(vowel='speaker', vowel_shape='fine', voiced_trim=-5, blend=True, release_ms=0, keep_mixed=True)
def sc(b):
    y = vl.synthesise(hb.SottoV38(**DEC).decode(b))[vl.ar.CODEC_DELAY:]; n = min(len(y), len(x8))
    return pesq(8000, x8[:n], y[:n], 'nb')
b0 = np.load(f'/tmp/claude-0/hiss/bits/{clip}_full.npy')
out = [f"v3.8 {sc(b0):.3f}"]
for sd, sv in ((-10.0, True), (-6.0, True), (-10.0, False)):
    e = hb.SottoV38(vowel='speaker', whisper=True, vsmooth=True, sib_db=sd); e.sib_voiced = sv
    b = e.encode(x); c, c0 = b[:, 0] * 2 + b[:, 1], b0[:, 0] * 2 + b0[:, 1]
    nv = int(((c != c0) & (c0 == 1)).sum()); nm = int(((c != c0) & (c0 != 1)).sum())
    out.append(f"sib{sd:+.0f}{'v' if sv else ' '} {sc(b):.3f}(V{nv}+M{nm})")
print(f"{clip:22s} " + '  '.join(out), flush=True)
