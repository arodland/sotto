import sys, glob, os; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, hiss_band as hb
D='/tmp/claude-0/data/ls/LibriSpeech/dev-clean'
TEST={'2428','251','3081','3536','6313','652','777','8842'}
LB=[(2000,2500),(2500,3000),(3000,3500),(3500,3900)]
A=[];Y=[]
for s in sorted(os.listdir(D)):
    if s in TEST: continue
    fs=sorted(glob.glob(f'{D}/{s}/*/*.flac'))[:6]
    x=np.concatenate([sf.read(f)[0] for f in fs])[:30*16000]
    P=np.abs(hb.stft(x))**2; H=hb.bands_db(P,hb.HB); L=hb.bands_db(P,LB); lo=hb.bands_db(P,[(0,1000)])[:,0]
    tot=10*np.log10(P.sum(1)+1e-12); vow=(lo-L[:,1]>10)&(tot>np.percentile(tot,60))
    slope=np.polyfit(np.array([2250,2750,3250])/1000, L[vow][:,:3].T,1)[0]
    A.append(np.column_stack([np.ones(vow.sum()),np.clip(slope,-30,10)])); Y.append(H[vow]-L[vow][:,2:3])
A=np.concatenate(A);Y=np.concatenate(Y)
C=np.linalg.lstsq(A,Y,rcond=None)[0]; r=Y-A@C
print('template a_b (dB re 3.0-3.5k):',C[0].round(1),' slope coef c_b:',C[1].round(2),' resid rms',np.sqrt((r**2).mean(0)).round(1), ' n',len(A))
np.save('vtemplate.npy',C)
