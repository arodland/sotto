"""Which v3.8 encoder rule costs PESQ on librispeech_6313_f (v3.7 2.929 -> v3.8 2.662)?"""
import sys; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb
from scipy.signal import resample_poly
from pesq import pesq
vl.TRACK2['lag']=8
clip=sys.argv[1] if len(sys.argv)>1 else 'librispeech_6313_f'
x=sf.read(f'/tmp/claude-0/hiss/clips16/{clip}.wav')[0]; x=x[:len(x)//320*320]; x8=resample_poly(x,1,2)
def score(b):
    y=vl.synthesise(hb.SottoV38().decode(b))[vl.ar.CODEC_DELAY:]; n=min(len(y),len(x8)); return pesq(8000,x8[:n],y[:n],'nb')
e=hb.SottoV38(); b=vl.SottoV37.encode(e,x8); print('v3.7 encode, v3.8 decode', round(score(b),3))
e._onsets(x8,b); print('+onset recode', round(score(b),3))
P=np.abs(hb.stft(x))**2; m=e.decode(b); _,_,f,_=hb.frame_map(m,b,len(P))
top,low=hb.bands_db(P,[(4000,8000)])[:,0],hb.bands_db(P,[(0,3900)])[:,0]
for t in np.where((top[1:]-top[:-1]>hb.BURST_DB)&(top[1:]>low[1:]-15))[0]+1: b[f[t],0]=b[f[t],1]=0
print('+burst rule', round(score(b),3))
e._whisper(x8,b); print('+whisper', round(score(b),3))
e._vsmooth(b); print('+vsmooth', round(score(b),3))
one=hb.bands_db(P,[(0,1000)])[:,0]; code=b[:,0]*2+b[:,1]
for t in np.where(top-one>3.0)[0]:
    g=f[t]
    if g<len(b) and code[g] in (2,3): b[g,0]=b[g,1]=0
print('+sibilant', round(score(b),3))
v=vl.SottoV37(); b7=v.encode(x8); y=vl.synthesise(v.decode(b7))[vl.ar.CODEC_DELAY:]; n=min(len(y),len(x8)); print('pure v3.7', round(pesq(8000,x8[:n],y[:n],'nb'),3))
