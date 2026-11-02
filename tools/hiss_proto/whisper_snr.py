"""Harmonic SNR below 1 kHz of frames v3.7 calls voiced: whispered vowels (bench) vs
real speech (test clips). Whispered frames labelled MIXED synthesise a junk-pitch buzz."""
import sys; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto/tools/hiss_proto')
import numpy as np, soundfile as sf, vocoder_lab as vl, sinusoid as sn, vowseg as vs, glob
from scipy.signal import resample_poly
vl.TRACK2['lag']=8
FS=8000
def snrs(x8):
    m=vl.analyse(x8, vl.SottoV37.PITCH); Sw=sn.spectra(np.round(x8*32767)); out=[]
    for k in range(len(m)):
        if m['voiced'][k]<=0: out.append(np.nan); continue
        wo=m['Wo'][k]; L=sn.n_harm(wo); f=np.arange(L+1)*wo*FS/(2*np.pi)
        out.append(sn.harmonic_snr(Sw[k],wo,L)[(f>0)&(f<1000)].mean())
    return np.array(out)
def q(a): a=a[~np.isnan(a)]; return f"n{len(a):4d} pct10/25/50/75/90 "+' '.join(f"{v:5.1f}" for v in np.percentile(a,[10,25,50,75,90])) if len(a) else 'none'
jx=resample_poly(sf.read('/tmp/claude-0/jeff/vowelvoice.flac')[0],1,6); jx=0.5*jx/np.abs(jx).max()
wx=resample_poly(vs.load16('/tmp/claude-0/wife/rec.wav'),1,2); wx=0.5*wx/np.abs(wx).max()
px=resample_poly(vs.load16('/tmp/claude-0/jeffphone/rec.wav'),1,2); px=0.5*px/np.abs(px).max()
s=snrs(jx); print('jeff ATR whisper 2-9s ', q(s[200:900])); print('jeff ATR voiced 9.5-25s', q(s[950:2500]))
s=snrs(wx); print('wife whisper 1-6.3,9.4-15', q(np.r_[s[100:630],s[940:1500]])); print('wife voiced 15.2-22.8', q(s[1520:2280]))
s=snrs(px); print('jeff phone whisper 5.2-11.2', q(s[520:1120])); print('jeff phone voiced', q(np.r_[s[90:390],s[1190:1520]]))
allr=[]
for c in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    x=resample_poly(sf.read(c)[0],1,2); allr.append(snrs(x))
allr=np.concatenate(allr); print('test clips (12) voiced frames', q(allr))
for t in (2,3,4,5,6): print(f'  test-clip voiced frames below {t} dB: {np.nanmean(allr<t)*100/np.mean(~np.isnan(allr)):.1f}%')

def runs(s, k=7):
    """Rolling median SNR over k frames (70 ms) of voiced frames: whisper stays low."""
    v=~np.isnan(s); out=np.full(len(s),np.nan)
    for i in np.where(v)[0]:
        w=s[max(0,i-k//2):i+k//2+1]; w=w[~np.isnan(w)]
        if len(w)>=k//2+1: out[i]=np.median(w)
    return out
print('--- rolling 70 ms median')
s=snrs(jx); r=runs(s); print('jeff ATR whisper', q(r[200:900]), ' voiced', q(r[950:2500]))
s=snrs(wx); r=runs(s); print('wife whisper', q(np.r_[r[100:630],r[940:1500]]), ' voiced', q(r[1520:2280]))
s=snrs(px); r=runs(s); print('jeff phone whisper', q(r[520:1120]))
R=np.concatenate([runs(snrs(resample_poly(sf.read(c)[0],1,2))) for c in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav'))])
print('test clips', q(R))
for t in (3,3.5,4,4.5,5): print(f'  thr {t}: test voiced frames hit {np.nansum(R<t)/np.sum(~np.isnan(R))*100:.1f}%')
