"""Whisper vs voice features on frames v3.7 calls voiced: harmonic SNR <1 kHz, pitch
jitter, and 80-300 Hz energy re 300-1000 Hz (no glottal source in a whisper)."""
import sys, os, pickle; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto/tools/hiss_proto')
import numpy as np, soundfile as sf, vocoder_lab as vl, sinusoid as sn, vowseg as vs, glob
from scipy.signal import resample_poly
vl.TRACK2['lag']=8; FS=8000
def feat(x8):
    m=vl.analyse(x8, vl.SottoV37.PITCH); Sw=sn.spectra(np.round(x8*32767)); n=len(m)
    snr=np.full(n,np.nan); lowr=np.full(n,np.nan)
    for k in range(n):
        P=np.abs(Sw[k][:len(Sw[k])//2+1])**2 if np.iscomplexobj(Sw[k]) else Sw[k]
        F=np.linspace(0,FS/2,len(P))
        lowr[k]=10*np.log10(P[(F>70)&(F<300)].sum()+1e-9)-10*np.log10(P[(F>300)&(F<1000)].sum()+1e-9)
        if m['voiced'][k]<=0: continue
        wo=m['Wo'][k]; L=sn.n_harm(wo); f=np.arange(L+1)*wo*FS/(2*np.pi)
        snr[k]=sn.harmonic_snr(Sw[k],wo,L)[(f>0)&(f<1000)].mean()
    jit=np.r_[0,np.abs(np.diff(np.log(m['Wo'])))]
    return dict(v=m['voiced']>0, snr=snr, lowr=lowr, jit=jit, en=10*np.log10(np.array([np.sum(np.abs(s)**2) for s in Sw])+1e-9))
C='/tmp/claude-0/hiss/whisper_feat.pkl'
if os.path.exists(C): D=pickle.load(open(C,'rb'))
else:
    D={}
    jx=resample_poly(sf.read('/tmp/claude-0/jeff/vowelvoice.flac')[0],1,6); D['jeffATR']=feat(0.5*jx/np.abs(jx).max())
    wx=resample_poly(vs.load16('/tmp/claude-0/wife/rec.wav'),1,2); D['wife']=feat(0.5*wx/np.abs(wx).max())
    px=resample_poly(vs.load16('/tmp/claude-0/jeffphone/rec.wav'),1,2); D['jeffphone']=feat(0.5*px/np.abs(px).max())
    for c in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')): D[os.path.basename(c)[:-4]]=feat(resample_poly(sf.read(c)[0],1,2))
    pickle.dump(D,open(C,'wb'))
