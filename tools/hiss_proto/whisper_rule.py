"""Score candidate whisper rules (rolling 70 ms medians of harmonic SNR <1 kHz and pitch jitter)."""
import pickle, numpy as np
D=pickle.load(open('/tmp/claude-0/hiss/whisper_feat.pkl','rb'))
def roll(a,k=7):
    out=np.full(len(a),np.nan)
    for i in range(len(a)):
        w=a[max(0,i-k//2):i+k//2+1]; w=w[~np.isnan(w)]
        if len(w): out[i]=np.median(w)
    return out
W={'jeffATR':[(200,900)],'wife':[(100,630),(940,1500)],'jeffphone':[(520,1120)]}
V={'jeffATR':[(950,2500)],'wife':[(1520,2280)],'jeffphone':[(90,390),(1190,1520)]}
R={k:{kk:roll(np.where(d['v'],d[kk],np.nan)) for kk in ('snr','jit')} for k,d in D.items()}
def sel(d,rng):
    idx=np.zeros(len(d['v']),bool)
    for a,b in rng: idx[a:b]=True
    return idx&d['v']
for ts,tj in [(5,0.02),(5,0.025),(6,0.02),(6,0.025),(6,0.03),(7,0.025),(5,0.015)]:
    out=[]
    for k in W:
        h=(R[k]['snr']<ts)&(R[k]['jit']>tj)
        out.append(f"{k} W{np.mean(h[sel(D[k],W[k])])*100:3.0f}% V{np.mean(h[sel(D[k],V[k])])*100:3.0f}%")
    t=[(R[k]['snr']<ts)&(R[k]['jit']>tj) for k in D if k not in W]; tv=[D[k]['v'] for k in D if k not in W]
    th=np.concatenate([a[b] for a,b in zip(t,tv)])
    print(f"snr<{ts} & jit>{tj}:",' | '.join(out),f"| test voiced {th.mean()*100:.1f}%")
