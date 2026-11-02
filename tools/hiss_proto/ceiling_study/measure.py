"""How well v3.8 reproduces each speaker's sibilant 4-8 kHz envelope (250 Hz bins)."""
import sys, os, glob, numpy as np, soundfile as sf
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'sim'))
import vocoder_lab as vl, hiss_band as hb
vl.TRACK2['lag'] = 8
ENC = dict(vowel='speaker', whisper=True, vsmooth=True, sib_db=-10.0, sib_voiced=True)
DEC = dict(vowel='speaker', vowel_shape='fine', voiced_trim=-5, blend=True, release_ms=0, keep_mixed=True)
FB = np.arange(4000, 8000, 250); FC = FB + 125
def fine(P): return np.stack([10*np.log10(P[:, (hb.FR >= a) & (hb.FR < a+250)].mean(1)+1e-12) for a in FB], 1)
def ceil4(P):
    """decoder's piecewise-linear dB interpolation of the TRUE 4 band levels -> 250 Hz bins"""
    L = hb.bands_db(P, hb.HB)
    lvl = np.stack([np.interp(hb.FR, hb.HBC, h) for h in L])
    return fine(10**(lvl/10))
def run(path, cache='/tmp/claude-0/sib/cache'):
    os.makedirs(cache, exist_ok=True)
    k = os.path.basename(path)[:-4]; c = f'{cache}/{k}.npz'
    x = sf.read(path)[0]; x = x[:len(x)//320*320]
    if os.path.exists(c):
        z = np.load(c); return x, z['bits'], z['y']
    b = hb.SottoV38(**ENC).encode(x); y = hb.SottoV38(**DEC).decode_audio16(b, n16=len(x))
    np.savez(c, bits=b, y=y); return x, b, y
def sib_mask(x, b):
    P = np.abs(hb.stft(x))**2
    tot = 10*np.log10(P.sum(1)+1e-12); lo = hb.bands_db(P, [(0, 1000)])[:, 0]; hi = hb.bands_db(P, [(4000, 8000)])[:, 0]
    nfr = len(P); f = np.clip(np.arange(nfr)+hb.HB_LAG, 0, 2*len(b)-1)//2
    code = b[:, 0]*2+b[:, 1]
    return P, (tot > np.percentile(tot, 97)-30) & (hi > lo+6) & (code[f] == 0)
if __name__ == '__main__':
    rows = []
    for p in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
        x, b, y = run(p)
        P, s = sib_mask(x, b)
        Q = np.abs(hb.stft(y[:len(x)]))**2; n = min(len(P), len(Q)); P, Q, s = P[:n], Q[:n], s[:n]
        O, D, C = fine(P[s]), fine(Q[s]), ceil4(P[s])
        # level-free shape: each frame re its own 4-8 kHz power
        nl = lambda F: F - 10*np.log10(np.mean(10**(F/10), 1))[:, None]
        lvl = (10*np.log10(np.mean(10**(D/10), 1)) - 10*np.log10(np.mean(10**(O/10), 1))).mean()
        bias = (nl(D) - nl(O)).mean(0)                      # speaker's mean shape miss
        rmsD = np.sqrt(((nl(D)-nl(O))**2).mean()); rmsC = np.sqrt(((nl(C)-nl(O))**2).mean())
        rmsDb = np.sqrt(((nl(D)-nl(O)-bias)**2).mean())     # after removing a per-speaker fixed shape
        k = os.path.basename(p)[:-4]
        rows.append((k, s.sum(), lvl, rmsD, rmsDb, rmsC, bias))
        print(f'{k:22s} n={s.sum():3d} level {lvl:+.1f} dB | shape rms decoded {rmsD:4.1f}  -spk-bias {rmsDb:4.1f}  ceiling(4 true bands) {rmsC:4.1f} | bias/kHz ' +
              ' '.join(f'{v:+.0f}' for v in bias.reshape(4, 4).mean(1)) + ' | 7.5-8k ' + f'{bias[-2:].mean():+.0f}', flush=True)
    import pickle; pickle.dump(rows, open("/tmp/claude-0/sib/measure.pkl","wb"))
