"""Sibilant rule (M/edge frames with 4-8 kHz > 0-1 kHz + 3 dB -> UU): PESQ-NB on the 12 test clips."""
import sys; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb, glob, os
from scipy.signal import resample_poly
from pesq import pesq
vl.TRACK2['lag']=8
tot=[]
for c in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    x=sf.read(c)[0]; x=x[:len(x)//320*320]; x8=resample_poly(x,1,2); r=[]; cs=[]
    for enc in (hb.SottoV38(whisper=True), hb.SottoV38(whisper=True, sib_db=3.0)):
        b,_,_=enc.encode_low(x); cs.append(b[:,0]*2+b[:,1])
        y=vl.synthesise(hb.SottoV38().decode(b))[vl.ar.CODEC_DELAY:]; n=min(len(y),len(x8)); r.append(pesq(8000,x8[:n],y[:n],'nb'))
    tot.append(r); print(os.path.basename(c),'changed %.1f%%'%(np.mean(cs[0]!=cs[1])*100),'PESQ %.3f -> %.3f'%tuple(r),flush=True)
    if 'p051' in c:
        s=lambda k: ''.join('.VMe'[v] for v in k[300:325]); print('  6.0-6.5s', s(cs[0]), '->', s(cs[1]))
print('mean PESQ %.3f -> %.3f'%tuple(np.mean(tot,0)))
