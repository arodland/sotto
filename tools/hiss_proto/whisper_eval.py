"""Whisper rule: frames changed on bench whispers vs test clips, and PESQ-NB on test clips."""
import sys; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto/tools/hiss_proto')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb, vowseg as vs, glob, os
from scipy.signal import resample_poly
from pesq import pesq
vl.TRACK2['lag']=8
def run(x16):
    a=hb.SottoV38(); b0,_,_=a.encode_low(x16)
    w=hb.SottoV38(whisper=True); b1,_,_=w.encode_low(x16)
    return b0,b1
jx=resample_poly(sf.read('/tmp/claude-0/jeff/vowelvoice.flac')[0],1,3); jx=0.5*jx/np.abs(resample_poly(jx,1,2)).max()
wx=vs.load16('/tmp/claude-0/wife/rec.wav'); wx=0.5*wx/np.abs(wx).max()
px=vs.load16('/tmp/claude-0/jeffphone/rec.wav'); px=0.5*px/np.abs(px).max()
code=lambda b: b[:,0]*2+b[:,1]
for nm,x,W,V in [('jeffATR',jx,[(2,9)],[(9.5,25)]),('wife',wx,[(1,6.3),(9.4,15)],[(15.2,22.8)]),('jeffphone',px,[(5.2,11.2)],[(0.9,3.9),(11.9,15.2)])]:
    x=x[:len(x)//320*320]; b0,b1=run(x); c0,c1=code(b0),code(b1)
    f=lambda R,c: np.concatenate([c[int(a*50):int(e*50)] for a,e in R])
    print(nm,'whisper frames UU: before %.0f%% after %.0f%%'%(np.mean(f(W,c0)==0)*100,np.mean(f(W,c1)==0)*100),' voiced section changed %.1f%%'%(np.mean(f(V,c0)!=f(V,c1))*100))
tot=[]
for c in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    x=sf.read(c)[0]; x=x[:len(x)//320*320]; b0,b1=run(x); ch=np.mean(code(b0)!=code(b1))*100
    x8=resample_poly(x,1,2); r=[]
    for b in (b0,b1):
        y=vl.synthesise(hb.SottoV38().decode(b))[vl.ar.CODEC_DELAY:]; n=min(len(y),len(x8)); r.append(pesq(8000,x8[:n],y[:n],'nb'))
    tot.append(r); print(os.path.basename(c),'changed %.1f%%'%ch,'PESQ %.3f -> %.3f'%tuple(r))
print('mean PESQ %.3f -> %.3f'%tuple(np.mean(tot,0)))
