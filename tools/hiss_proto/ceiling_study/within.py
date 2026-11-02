"""Per-speaker within-band sibilant shape: mean of (true 250 Hz bins - decoder's interpolation
of the true 4 band levels) over sibilant frames. That is what the per-frame index can't carry."""
import sys, glob, os, numpy as np, soundfile as sf, pickle
from scipy.signal import resample_poly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from measure import fine, ceil4, hb
LS='/tmp/claude-0/data/ls/LibriSpeech/dev-clean'
nl = lambda F: F - 10*np.log10(np.mean(10**(F/10), 1))[:, None]
def frames(x):
    P = np.abs(hb.stft(x))**2
    tot = 10*np.log10(P.sum(1)+1e-12); lo = hb.bands_db(P, [(0,1000)])[:,0]; hi = hb.bands_db(P, [(4000,8000)])[:,0]
    return P[(tot > np.percentile(tot, 97)-30) & (hi > lo+6)]
def spk_files():
    for s in sorted(os.listdir(LS), key=int):
        yield 'ls'+s, sorted(glob.glob(f'{LS}/{s}/*/*.flac'))[:40], 16000
    for p in ('p020','p047','p051','p095'):
        yield 'ears'+p, sorted(f for f in glob.glob(f'/tmp/claude-0/data/ears/{p}/*.wav') if 'regular' in f or 'sentences' in f)[:40], 48000
out = {}
for k, fs, sr in spk_files():
    Ps = []
    for f in fs:
        x = sf.read(f)[0]
        if sr != 16000: x = resample_poly(x, 16000, sr)
        Ps.append(frames(x))
    P = np.concatenate(Ps)
    if len(P) < 50: print(k, 'too few', len(P)); continue
    O, C = fine(P), ceil4(P)
    wb = (nl(O) - nl(C))                       # per frame within-band miss
    # split half reliability: is the speaker mean stable?
    h = len(wb)//2; m1, m2 = wb[:h].mean(0), wb[h:].mean(0)
    out[k] = dict(mean=wb.mean(0), n=len(wb), rms_frame=np.sqrt((wb**2).mean()), rms_after=np.sqrt(((wb-wb.mean(0))**2).mean()), split=np.corrcoef(m1, m2)[0,1])
    print(f'{k:9s} n={len(wb):5d} frame rms {out[k]["rms_frame"]:4.1f} -> {out[k]["rms_after"]:4.1f} after speaker mean | split-half r {out[k]["split"]:+.2f} | ' + ' '.join(f'{v:+3.0f}' for v in wb.mean(0)), flush=True)
pickle.dump(out, open('/tmp/claude-0/sib/within.pkl','wb'))
