"""What differs in decoded s frames: alignment, peak sharpness, flatness, 0-4 kHz texture, onsets."""
import sys, glob, os, numpy as np
from scipy.signal import butter, sosfiltfilt, hilbert
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from measure import run, sib_mask, hb, fine
def env(z, lo, hi, fs=16000, sm=32):
    e = np.abs(hilbert(sosfiltfilt(butter(6, [lo, hi], 'band', fs=fs, output='sos'), z)))
    return e
rows = []
for p in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    k = os.path.basename(p)[:-4]
    if '3170' in k: continue
    x, b, y = run(p); y = y[:len(x)]
    # alignment of the 4-8 kHz envelope
    ex, ey = env(x, 4100, 7800), env(y, 4100, 7800)
    sm = lambda e: np.convolve(e, np.ones(80)/80, 'same')[::40]
    a, c = sm(ex), sm(ey); lags = range(-20, 21)
    cc = [np.corrcoef(a[max(0,l):len(a)+min(0,l)], c[max(0,-l):len(c)-max(0,l)])[0,1] for l in lags]
    lag = list(lags)[int(np.argmax(cc))] * 40
    P, s = sib_mask(x, b); Q = np.abs(hb.stft(y))**2; n = min(len(P), len(Q)); P, Q, s = P[:n], Q[:n], s[:n]
    if s.sum() < 5: continue
    hi = (hb.FR >= 4000) & (hb.FR < 8000)
    def stats(M):
        S = M[s][:, hi]
        flat = np.exp(np.mean(np.log(S + 1e-20), 1)) / S.mean(1)               # spectral flatness 4-8 kHz (50 Hz bins)
        F = fine(M[s]); prom = F.max(1) - 10*np.log10(np.mean(10**(F/10), 1))  # peak 250 Hz bin re band mean
        pk = 4125 + 250*np.argmax(F, 1)
        return 10*np.log10(flat).mean(), prom.mean(), pk
    fx, px, kx = stats(P); fy, py, ky = stats(Q)
    # texture: envelope modulation of 2.5-3.9 kHz and 4-8 kHz inside s frames: power at 40-110 Hz re total AC
    def modspec(z, lo, hi_):
        e = env(z, lo, hi_); idx = np.where(np.repeat(s, 160)[:len(e)])[0]
        segs = [e[i:i+320] for i in idx[::320] if i+320 <= len(e)]
        if not segs: return np.nan, np.nan
        E = np.mean([np.abs(np.fft.rfft(g - g.mean()))**2 for g in segs], 0); fm = np.fft.rfftfreq(320, 1/16000)
        kurt = np.mean([np.mean((g-g.mean())**4)/np.var(g)**2 for g in segs])
        return 10*np.log10(E[(fm >= 40) & (fm <= 110)].sum()/E[(fm > 0) & (fm <= 400)].sum()), kurt
    m_xl, k_xl = modspec(x, 2500, 3900); m_yl, k_yl = modspec(y, 2500, 3900)
    m_xh, k_xh = modspec(x, 4100, 7800); m_yh, k_yh = modspec(y, 4100, 7800)
    rows.append((fx, fy, px, py, np.mean(np.abs(kx-ky)), m_xl, m_yl, m_xh, m_yh))
    print(f'{k:20s} n={s.sum():3d} lag {lag:+4d} smp | flatness dB orig {fx:5.1f} dec {fy:5.1f} | peak prominence orig {px:4.1f} dec {py:4.1f} | peak freq miss {np.mean(np.abs(kx-ky)):4.0f} Hz | 50-100 Hz env mod 2.5-3.9k orig {m_xl:5.1f} dec {m_yl:5.1f}, 4-8k orig {m_xh:5.1f} dec {m_yh:5.1f} dB', flush=True)
r = np.nanmean(rows, 0); print('mean', np.round(r, 1))
