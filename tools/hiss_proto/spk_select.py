"""Side-channel speaker shape: which causal vowel-frame selection matches the oracle
(top 40% by energy among vowel-like frames over the whole clip)?"""
import sys, glob, os; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, hiss_band as hb
F=hb.fine_model()
def feats(x16, y16):
    nfr=min((len(y16)-hb.N)//hb.H+1,(len(x16)-hb.N)//hb.H+1)
    P=np.abs(hb.stft(x16)[:nfr])**2
    Y=hb.bands_db(P,hb.FHI)-hb.bands_db(P,[(3000,3500)])[:,0][:,None]
    pr,_=hb.fine_predict(np.abs(hb.stft(y16)[:nfr])**2)
    lo=hb.bands_db(P,[(0,1000)])[:,0]; tot=10*np.log10(P.sum(1)+1e-12)
    vr=lo-hb.bands_db(P,[(2500,3000)])[:,0]>10
    return Y-pr, vr, tot
def pc_of(miss, sel): return F['Vt']@(miss[sel].mean(0)-F['m0']) if sel.sum()>=20 else np.zeros(3)
def follower(tot, decay, db):
    pk=-1e9; ref=np.zeros(len(tot))
    for t,v in enumerate(tot): pk=max(v,pk-decay); ref[t]=pk
    return tot>ref-db
def runpct(tot, q, warm=100):
    out=np.zeros(len(tot),bool)
    for t in range(len(tot)):
        if t>=warm: out[t]=tot[t]>np.percentile(tot[:t+1],q)
    return out
import soundfile as sf
from v7_cache import sources
S=sources()
rules={'oracle':lambda tot:tot>np.percentile(tot,60)}
for dec in (0.05,0.02): 
    for db in (6,8,10): rules[f'pk{dec}-{db}']=lambda tot,dec=dec,db=db: follower(tot,dec,db)
for q in (50,60,70): rules[f'runpct{q}']=lambda tot,q=q: runpct(tot,q)
err={k:[] for k in rules}
for name in sorted(S):
    f=f'/tmp/claude-0/hiss/bits/{name}.npz'
    if not os.path.exists(f): continue
    z=np.load(f); miss,vr,tot=feats(S[name],z['y16'])
    ref=pc_of(miss, vr&rules['oracle'](tot)); row=[]
    for k,r in rules.items():
        pc=pc_of(miss, vr&r(tot)); e=np.sqrt(((F['Vt'].T@(pc-ref))**2).mean()); err[k].append(e); row.append(f"{k} {pc.round(0)} {e:4.1f}")
    print(name, '| '.join(row))
print('mean band rms dB vs oracle:', {k:round(float(np.mean(v)),1) for k,v in err.items()})
