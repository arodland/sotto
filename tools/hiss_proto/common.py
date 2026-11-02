import sys, os, glob, numpy as np, soundfile as sf
sys.path.insert(0, '/home/claude/sotto/sim')
import vocoder_lab as vl
from scipy.signal import resample_poly

N, H = 320, 160                       # 20 ms window, 10 ms hop at 16 kHz
WIN = np.sqrt(np.hanning(N + 1)[:N])  # sqrt-Hann, squares sum to 1 at 50% overlap
FR = np.fft.rfftfreq(N, 1 / 16000)
LB = [(0, 1000), (1000, 2000), (2000, 2500), (2500, 3000), (3000, 3500), (3500, 3900)]
HB = [(4000, 5000), (5000, 6000), (6000, 7000), (7000, 8000)]
HBC = np.array([4500, 5500, 6500, 7500.])

def stft(x):
    n = (len(x) - N) // H + 1
    fr = np.stack([x[i * H:i * H + N] * WIN for i in range(n)])
    return np.fft.rfft(fr, axis=1)

def istft(X, n):
    y = np.zeros(n + N)
    fr = np.fft.irfft(X, N, axis=1) * WIN
    for i, f in enumerate(fr):
        y[i * H:i * H + N] += f
    return y[:n]

def bands_db(P, bands):
    return np.stack([10 * np.log10(P[:, (FR >= a) & (FR < b)].mean(1) + 1e-12) for a, b in bands], 1)

def codec_run(x16, v=None, uv_gain=0.0, mix_gain=0.0):
    """-> (decoded 8 kHz aligned to input, model frames, bits). uv_gain: dB on unvoiced
    frames; mix_gain: dB on the noise harmonics of mixed frames."""
    v = v or vl.SottoV36()
    x8 = resample_poly(x16, 1, 2)
    bits = v.encode(x8)
    m = v.decode(bits)
    m = level_fix(m, uv_gain, mix_gain)
    y = vl.synthesise(m)[160:]
    y = np.pad(y, (0, max(0, len(x8) - len(y))))[:len(x8)]
    return x8, y, m, bits

def level_fix(m, uv_gain, mix_gain):
    if not uv_gain and not mix_gain:
        return m
    m = m.copy()
    for k in range(len(m)):
        L, vv = m[k]['L'], m[k]['voiced']
        if vv == 0:
            m[k]['A'][1:L + 1] *= 10 ** (uv_gain / 20)
        elif vv >= 2:
            m[k]['A'][vv:L + 1] *= 10 ** (mix_gain / 20)
    return m

def frame_voicing(m, nfr, lag):
    """voicing class (0 U, 1 V, 2 M) for STFT frame t = model frame t + lag."""
    vv = np.minimum(m['voiced'], 2)
    out = np.zeros(nfr, int)
    for t in range(nfr):
        k = min(max(t + lag, 0), len(vv) - 1)
        out[t] = vv[k]
    return out

LAG = None
def features(x16, uv_gain=0.0, mix_gain=0.0, lag=None):
    x8, y8, m, bits = codec_run(x16, uv_gain=uv_gain, mix_gain=mix_gain)
    y16 = resample_poly(y8, 2, 1)
    n = min(len(x16), len(y16))
    X, Y = stft(x16[:n]), stft(y16[:n])
    PX, PY = np.abs(X) ** 2, np.abs(Y) ** 2
    d = dict(x16=x16[:n], y16=y16[:n], m=m, bits=bits, X=X, Y=Y,
             lb=bands_db(PY, LB), hb=bands_db(PX, HB), lbo=bands_db(PX, LB))
    return d

def attach_voicing(d, lag):
    nfr = len(d['X'])
    d['v'] = frame_voicing(d['m'], nfr, lag)
    code = d['bits'][:, 0] * 2 + d['bits'][:, 1]
    k = np.clip(np.arange(nfr) + lag, 0, 2 * len(code) - 1)
    d['uu'] = code[k // 2] == 0
    d['frame'] = k // 2
    return d

FB = np.array([1250., 1750, 2250, 2750, 3250, 3600, 3800])   # model envelope probe points
def model_env(m):
    """dB envelope of each decoded model frame at FB (harmonic powers, interpolated)."""
    out = np.zeros((len(m), len(FB)))
    for k, r in enumerate(m):
        L = r['L']; f = np.arange(1, L + 1) * r['Wo'] * 4000 / np.pi
        a = 20 * np.log10(r['A'][1:L + 1] + 1e-9)
        out[k] = np.interp(FB, f, a)
    return out

def model_feats(d, lag=2):
    """per STFT frame t: envelope at FB from model frame t+lag, voicing class."""
    E = model_env(d['m']); nfr = len(d['X'])
    k = np.clip(np.arange(nfr) + lag, 0, len(E) - 1)
    return E[k]
