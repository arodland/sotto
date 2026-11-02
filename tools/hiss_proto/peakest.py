import sys; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, hiss_band as hb
from scipy.signal import resample_poly
def peak_of(x16):
    P=np.abs(hb.stft(x16))**2; lo=hb.bands_db(P,[(0,1000)])[:,0]; hi=hb.bands_db(P,[(2500,4000)])[:,0]
    tot=10*np.log10(P.sum(1)+1e-12); vow=(lo-hi>10)&(tot>np.percentile(tot,60))
    S=10*np.log10(P[vow].mean(0)+1e-12); S=np.convolve(S,np.ones(5)/5,'same')
    band=(hb.FR>4500)&(hb.FR<7500); f=hb.FR[band][np.argmax(S[band])]
    floor=np.median(S[band]); return float(f), float(S[band].max()-floor)
if __name__=='__main__':
    x,fs=sf.read('/tmp/claude-0/jeff/vowelvoice.flac'); x=resample_poly(x,1,3)
    print('jeff', peak_of(x))
    for c in ['librispeech_8842_f','ears_p051_f','ears_p047_f','ears_p020_m']:
        print(c, peak_of(sf.read(f'/tmp/claude-0/hiss/clips16/{c}.wav')[0]))
