"""Can the vowel shape below 4 kHz predict where the 4-8 kHz resonance sits?
Ridge from 250 Hz bands (250-3900 Hz, re 3.0-3.5 kHz) to 250 Hz bands 4-8 kHz.
Trained on LibriSpeech dev-clean (minus test speakers), scored on held-out LS
speakers and the two bench recordings (Jeff, Jeff's wife)."""
import sys, glob, os; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, hiss_band as hb, vowseg as vs, pickle
D='/tmp/claude-0/data/ls/LibriSpeech/dev-clean'
TEST={'2428','251','3081','3536','6313','652','777','8842'}
LO=[(f,f+250) for f in range(250,3750,250)]+[(3750,3950)]
HI=[(f,f+250) for f in range(4000,8000,250)]; HC=np.array([a+125 for a,_ in HI])
REF=(3000,3500)
def feats(x, sel=None):
    P=np.abs(hb.stft(x))**2; L=hb.bands_db(P,LO); H=hb.bands_db(P,HI); r=hb.bands_db(P,[REF])[:,0]
    lo=hb.bands_db(P,[(0,1000)])[:,0]; tot=10*np.log10(P.sum(1)+1e-12)
    vow=(lo-hb.bands_db(P,[(2500,3000)])[:,0]>10)&(tot>np.percentile(tot,60)) if sel is None else sel
    return L[vow]-r[vow,None], H[vow]-r[vow,None]
C='/tmp/claude-0/hiss/vfine.pkl'
if os.path.exists(C): tr,te=pickle.load(open(C,'rb'))
else:
    tr={};te={}
    for s in sorted(os.listdir(D)):
        fs=sorted(glob.glob(f'{D}/{s}/*/*.flac'))[:6]; x=np.concatenate([sf.read(f)[0] for f in fs])[:30*16000]
        (te if s in TEST else tr)[s]=feats(x)
    pickle.dump((tr,te),open(C,'wb'))
X=np.concatenate([v[0] for v in tr.values()]); Y=np.concatenate([v[1] for v in tr.values()])
A=np.column_stack([np.ones(len(X)),X]); lam=50*np.eye(A.shape[1]); lam[0,0]=0
Wr=np.linalg.solve(A.T@A+lam,A.T@Y); mu=Y.mean(0)
print('train n',len(X))
def pk(y):
    s=np.convolve(y,[.25,.5,.25],'same'); i=np.argmax(s[1:-1])+1; return HC[i], s[i]-np.median(s)
def score(name,X,Y):
    pr=np.column_stack([np.ones(len(X)),X])@Wr; spk=(Y-pr).mean(0)
    M={'mean':np.repeat(mu[None],len(X),0),'vowel':pr,'vowel+spk':pr+spk,'spk const':np.repeat(Y.mean(0)[None],len(X),0)}
    out=[]
    for k,p in M.items():
        rms=np.sqrt(((Y-p)**2).mean()); pe=[];
        for y,q in zip(Y,p):
            fy,hy=pk(y)
            if hy>6: pe.append(abs(fy-pk(q)[0]))
        out.append(f"{k} {rms:4.1f}dB/{np.median(pe) if pe else np.nan:5.0f}Hz")
    print(f"{name:10s} n{len(X):5d} ", ' | '.join(out))
for s,(x,y) in te.items(): score('LS '+s,x,y)
print('LS test pooled per-speaker averages above; bench:')
for nm,p,segs in [('jeff','/tmp/claude-0/jeff/vowelvoice.flac',None),('wife','/tmp/claude-0/wife/rec.wav',[(1.15,2.1),(2.25,2.9),(3.4,4.0),(4.45,5.0),(5.6,6.2),(9.5,10.1),(10.7,11.3),(11.85,12.4),(13.1,13.7),(14.3,14.9),(18.6,19.1),(19.45,20.0),(20.2,20.7),(21.5,22.0),(22.2,22.7)])]:
    x=vs.load16(p); n=len(x)//160+1
    if segs is None:
        P=np.abs(hb.stft(x))**2; _,sg=vs.segments(x); segs=[(a/100,b/100) for a,b in sg]
    sel=np.zeros(len(hb.stft(x)),bool)
    for a,b in segs: sel[int(a*100)+5:int(b*100)-5]=True
    score(nm,*feats(x,sel))
np.save('/tmp/claude-0/hiss/vfine_W.npy',Wr)
