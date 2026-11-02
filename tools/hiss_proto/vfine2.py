"""Per-speaker correction to the vowel-driven 4-8 kHz predictor, compressed to a few
PCA coefficients that could ride the slow side channel. Errors on 5-frame smoothed spectra."""
import numpy as np, pickle, vowseg as vs, hiss_band as hb
from vfine import tr, te, feats, HC
def sm(Y,k=5): return np.apply_along_axis(lambda c: np.convolve(c,np.ones(k)/k,'same'),0,Y)
def fit(D):
    X=np.concatenate([v[0] for v in D.values()]); Y=np.concatenate([v[1] for v in D.values()])
    A=np.column_stack([np.ones(len(X)),X]); lam=50*np.eye(A.shape[1]); lam[0,0]=0
    return np.linalg.solve(A.T@A+lam,A.T@Y)
pred=lambda W,X: np.column_stack([np.ones(len(X)),X])@W
# LOSO over training speakers for PCA basis of speaker offsets
W=fit(tr); off={s:(v[1]-pred(W,v[0])).mean(0) for s,v in tr.items()}
O=np.array(list(off.values())); m0=O.mean(0); U,S,Vt=np.linalg.svd(O-m0,full_matrices=False)
print('PCA var explained', (S**2/np.sum(S**2)).cumsum()[:5].round(2))
bench={}
bench['jeff']=None
def benchfeats():
    out={}
    x=vs.load16('/tmp/claude-0/jeff/vowelvoice.flac'); _,sg=vs.segments(x); sel=np.zeros(len(hb.stft(x)),bool)
    for a,b in sg: sel[a+5:b-5]=True
    out['jeff']=feats(x,sel)
    x=vs.load16('/tmp/claude-0/wife/rec.wav'); sel=np.zeros(len(hb.stft(x)),bool)
    for a,b in [(1.15,2.1),(2.25,2.9),(3.4,4.0),(4.45,5.0),(5.6,6.2),(9.5,10.1),(10.7,11.3),(11.85,12.4),(13.1,13.7),(14.3,14.9),(18.6,19.1),(19.45,20.0),(20.2,20.7),(21.5,22.0),(22.2,22.7)]: sel[int(a*100)+5:int(b*100)-5]=True
    out['wife']=feats(x,sel); return out
ev=dict(te); ev.update(benchfeats())
print(f"{'speaker':8s}  flat-tmpl  vowel  +k=0(mean off)  +k=1  +k=2  +k=3  +full   spk-const")
tot=[]
for s,(X,Y) in ev.items():
    Ys=sm(Y); p=pred(W,X); r=(Y-p).mean(0)-m0; row=[]
    # flat template = constant LS mean of Y
    mu=np.concatenate([v[1] for v in tr.values()]).mean(0)
    row.append(np.sqrt(((Ys-mu)**2).mean()))
    row.append(np.sqrt(((Ys-sm(p))**2).mean()))
    for k in (0,1,2,3):
        c=Vt[:k]@r; corr=m0+Vt[:k].T@c
        row.append(np.sqrt(((Ys-sm(p+corr))**2).mean()))
    row.append(np.sqrt(((Ys-sm(p+m0+r))**2).mean()))
    row.append(np.sqrt(((Ys-Y.mean(0))**2).mean()))
    tot.append(row); print(f"{s:8s} "+'  '.join(f"{v:6.1f}" for v in row))
print('mean     '+'  '.join(f"{v:6.1f}" for v in np.mean(tot,0)))
pickle.dump(dict(W=W,m0=m0,Vt=Vt[:4]),open('/tmp/claude-0/hiss/vfine_model.pkl','wb'))
