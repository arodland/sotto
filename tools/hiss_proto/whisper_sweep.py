"""Sweep whisper-rule thresholds offline (cached codes + features)."""
import sys, os, pickle; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto/tools/hiss_proto')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb, vowseg as vs, sinusoid as sn, glob
from scipy.signal import resample_poly
vl.TRACK2['lag']=8
C='/tmp/claude-0/hiss/whisper_sweep.pkl'
if os.path.exists(C): D=pickle.load(open(C,'rb'))
else:
    D={}
    jx=resample_poly(sf.read('/tmp/claude-0/jeff/vowelvoice.flac')[0],1,3); jx=0.5*jx/np.abs(resample_poly(jx,1,2)).max()
    wx=vs.load16('/tmp/claude-0/wife/rec.wav'); wx=0.5*wx/np.abs(wx).max()
    px=vs.load16('/tmp/claude-0/jeffphone/rec.wav'); px=0.5*px/np.abs(px).max()
    src={'jeffATR':jx,'wife':wx,'jeffphone':px}
    for c in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')): src[os.path.basename(c)[:-4]]=sf.read(c)[0]
    for k,x in src.items():
        x=x[:len(x)//320*320]; a=hb.SottoV38(); x8=resample_poly(x,1,2)
        bits=vl.SottoV37.encode(a,x8); a._onsets(x8,bits)
        m=vl.analyse(x8,a.PITCH); Sw=sn.spectra(np.round(x8*32767)); n=min(len(m),len(Sw)); snr=np.full(n,np.nan)
        for i in range(n):
            if m['voiced'][i]>0:
                wo=m['Wo'][i]; L=sn.n_harm(wo); fh=np.arange(L+1)*wo*4000/np.pi; snr[i]=sn.harmonic_snr(Sw[i],wo,L)[(fh>0)&(fh<1000)].mean()
        en=10*np.log10(np.array([np.sum(np.abs(s)**2) for s in Sw[:n]])+1e-9)
        D[k]=dict(code=bits[:,0]*2+bits[:,1],snr=snr,jt=np.r_[0,np.abs(np.diff(np.log(m['Wo'][:n])))],en=en)
    pickle.dump(D,open(C,'wb'))
def apply(d,mf,ts,tj):
    code=d['code'].copy(); g=0; n=len(d['snr'])
    while g<len(code):
        if code[g] not in (2,3): g+=1; continue
        h=g
        while h<len(code) and code[h] in (2,3): h+=1
        if h-g>=mf:
            sl=slice(2*g,min(2*h,n))
            if np.nanmedian(d['snr'][sl])<ts and np.median(d['jt'][sl])>tj: code[g:h]=0
        g=h
    return code
W={'jeffATR':[(2,9)],'wife':[(1,6.3),(9.4,15)],'jeffphone':[(5.2,11.2)]}
f=lambda R,c: np.concatenate([c[int(a*50):int(e*50)] for a,e in R])
if __name__=='__main__':
    for mf,ts,tj in [(5,6,.02),(5,5,.025),(8,6,.02),(8,5,.025),(10,5,.03),(10,6,.025),(15,6,.02)]:
        w=[np.mean(f(W[k],apply(D[k],mf,ts,tj))==0)*100 for k in W]
        t=[np.mean(apply(D[k],mf,ts,tj)!=D[k]['code'])*100 for k in D if k not in W]
        print(f"min{mf} snr<{ts} jit>{tj}: whisper UU "+' '.join(f'{v:3.0f}%' for v in w)+f" | test changed mean {np.mean(t):4.1f}% max {np.max(t):4.1f}%")
