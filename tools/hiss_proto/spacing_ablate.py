"""Unvoiced noise spacing in the v3.8 decoder hook, on v3.7 bits: v3.7 (100 Hz, no boost) vs
the hook at 50/75/100 Hz with the gated +3 dB boost, and 100 Hz without it."""
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
out=[('v3.7',sc(v.decode(b)))]
for hz,gain in ((50,3.0),(75,3.0),(100,3.0),(100,0.0),(100,1.5)):
    hb.W50=2*np.pi*hz/8000; hb.UV_GAIN=gain; out.append((f'{hz}Hz+{gain}',sc(hb.SottoV38().decode(b))))
print(f"{clip:22s} "+'  '.join(f"{k} {s:.3f}" for k,s in out))
