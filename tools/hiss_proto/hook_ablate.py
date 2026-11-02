"""The v3.8 8 kHz decoder hook (50 Hz noise spacing in unvoiced halves + gated +3 dB) on
v3.7 bits: PESQ-NB per clip for no hook / spacing only / spacing + boost."""
import sys; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb
from scipy.signal import resample_poly
from pesq import pesq
vl.TRACK2['lag']=8
clip=sys.argv[1]
x=sf.read(f'/tmp/claude-0/hiss/clips16/{clip}.wav')[0]; x=x[:len(x)//320*320]; x8=resample_poly(x,1,2)
v=vl.SottoV37(); b=v.encode(x8)
def sc(m):
    y=vl.synthesise(m)[vl.ar.CODEC_DELAY:]; n=min(len(y),len(x8)); return pesq(8000,x8[:n],y[:n],'nb')
r=[sc(v.decode(b))]
hb.UV_GAIN=0.0; r.append(sc(hb.SottoV38().decode(b)))
hb.UV_GAIN=3.0; r.append(sc(hb.SottoV38().decode(b)))
hb.UV_GAIN=1.5; r.append(sc(hb.SottoV38().decode(b)))
print(f"{clip:22s} v3.7 {r[0]:.3f}  spacing {r[1]:.3f}  +3dB {r[2]:.3f}  +1.5dB {r[3]:.3f}")
