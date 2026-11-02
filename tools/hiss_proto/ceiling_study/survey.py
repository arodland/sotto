"""Sibilant 3-8 kHz envelope survey on LibriSpeech dev-clean (16 kHz originals)."""
import sys, glob, os, numpy as np, soundfile as sf
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'sim'))
import hiss_band as hb
LS = '/tmp/claude-0/data/ls/LibriSpeech/dev-clean'
FB = np.arange(3000, 8000, 250)          # 250 Hz bins 3.0-8.0 kHz
FC = FB + 125

def fine(P):
    return np.stack([10*np.log10(P[:, (hb.FR >= a) & (hb.FR < a+250)].mean(1)+1e-12) for a in FB], 1)

def sib_frames(x, ex=6.0):
    P = np.abs(hb.stft(x))**2
    tot = 10*np.log10(P.sum(1)+1e-12)
    lo = hb.bands_db(P, [(0, 1000)])[:, 0]
    hi = hb.bands_db(P, [(4000, 8000)])[:, 0]
    mid = hb.bands_db(P, [(2000, 4000)])[:, 0]
    loud = tot > np.percentile(tot, 97) - 30
    s = loud & (hi > lo + ex)            # 4-8 kHz dominates: s, z, (f, th weaker)
    sh = loud & (mid > lo + ex) & (mid > hi + 3)
    return P, s, sh

def describe(m):
    """m: mean fine spectrum (dB) over FC. -> peak Hz, -10/-20 dB upper edge, slope 6-8 kHz dB/kHz"""
    k = np.argmax(m[FC >= 3500]) + np.sum(FC < 3500)
    pk = m[k]
    above = np.arange(len(m)) > k
    e10 = FC[above & (m < pk-10)]
    e20 = FC[above & (m < pk-20)]
    sl = np.polyfit(FC[FC >= 6000]/1000, m[FC >= 6000], 1)[0]
    return FC[k], (e10[0] if len(e10) else 8000), (e20[0] if len(e20) else 8000), sl

if __name__ == '__main__':
    out = {}
    for spk in sorted(os.listdir(LS), key=int):
        fs = sorted(glob.glob(f'{LS}/{spk}/*/*.flac'))[:40]
        S, SH = [], []
        for f in fs:
            x = sf.read(f)[0]
            P, s, sh = sib_frames(x)
            F = fine(P)
            S.append(F[s]); SH.append(F[sh])
        S = np.concatenate(S); SH = np.concatenate(SH)
        # normalise each frame to its 4-8 kHz mean power level before averaging shapes
        lvl = 10*np.log10(np.mean(10**(S[:, FB >= 4000]/10), 1))
        ms = 10*np.log10(np.mean(10**((S - lvl[:, None])/10), 0))
        d = describe(ms)
        out[spk] = dict(n=len(S), mean=ms, desc=d)
        print(f'{spk:>5} n={len(S):4d} peak {d[0]:.0f} -10dB@{d[1]:.0f} -20dB@{d[2]:.0f} slope6-8 {d[3]:+.1f} dB/kHz | ' +
              ' '.join(f'{v:+.0f}' for v in ms[::2]), flush=True)
    np.save('/tmp/claude-0/sib/survey_ls.npy', out, allow_pickle=True)
