"""Segment a bench recording of sustained vowels (voiced and whispered) and
report each vowel's 3-8 kHz shape: level of 4-8 kHz relative to 3-4 kHz and
resonance peaks above 4 kHz. Usage: vowseg.py FILE [t0 t1]"""
import sys; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf
from scipy.signal import resample_poly, find_peaks
import hiss_band as hb
def load16(p):
    x,fs=sf.read(p); x=x if x.ndim==1 else x.mean(1)
    from math import gcd; g=gcd(16000,fs); return resample_poly(x,16000//g,fs//g)
def segments(x16, minlen=25):
    P=np.abs(hb.stft(x16))**2; tot=10*np.log10(P.sum(1)+1e-12)
    on=tot>np.percentile(tot,30)+15
    segs=[]; t=0
    while t<len(on):
        if on[t]:
            u=t
            while u<len(on) and on[u]: u+=1
            if u-t>=minlen: segs.append((t,u))
            t=u
        else: t+=1
    return P,segs
def describe(P,a,b):
    Q=P[a+5:b-5]; S=10*np.log10(Q.mean(0)+1e-12); FR=hb.FR
    lo=10*np.log10(Q[:,(FR>80)&(FR<400)].mean()+1e-12); mid=10*np.log10(Q[:,(FR>400)&(FR<1500)].mean()+1e-12)
    # harmonicity: spectral flatness in 100-1500 Hz on single frames
    band=(FR>100)&(FR<1500); fl=np.mean([np.exp(np.mean(np.log(q[band]+1e-12)))/np.mean(q[band]) for q in Q])
    ref=10*np.log10(Q[:,(FR>=3000)&(FR<4000)].mean()+1e-12)
    hi=[10*np.log10(Q[:,(FR>=f)&(FR<f+1000)].mean()+1e-12)-ref for f in (4000,5000,6000,7000)]
    Ss=np.convolve(S,np.ones(5)/5,'same'); m=(FR>4000)&(FR<7900)
    pk,pr=find_peaks(Ss[m],prominence=3); fpk=[(int(FR[m][i]),round(float(p),1)) for i,p in zip(pk,pr['prominences'])]
    fpk=sorted(fpk,key=lambda z:-z[1])[:3]
    return dict(t=(a*0.01,b*0.01), voiced=fl<0.25, flat=round(float(fl),2), rel=[round(h,1) for h in hi], peaks=fpk)
if __name__=='__main__':
    x=load16(sys.argv[1]);
    if len(sys.argv)>3: x=x[int(float(sys.argv[2])*16000):int(float(sys.argv[3])*16000)]
    P,segs=segments(x)
    for a,b in segs:
        d=describe(P,a,b); print(f"{d['t'][0]:5.2f}-{d['t'][1]:5.2f} {'V' if d['voiced'] else 'W'} flat {d['flat']:.2f} rel4-8k {d['rel']} peaks {d['peaks']}")
