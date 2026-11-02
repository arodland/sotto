import sys, glob, numpy as np, soundfile as sf
sys.path.insert(0, '/home/claude/sotto/sim')
import vocoder_lab as vl
from scipy.signal import stft
v = vl.SottoV36()
clips = sorted(glob.glob('/mnt/project-files/vocoder/samples-clean/*.flac'))
B = np.array([0,1000,2000,2500,3000,3500,4000])
def spec(x):
    _, _, X = stft(x, 8000, nperseg=256, noverlap=176); return np.linspace(0,4000,X.shape[0]), np.abs(X)**2
S = {}
for f in clips:
    x, _ = sf.read(f)
    m0 = vl.analyse(x, 'tracked3')
    if len(m0) % 2: m0 = np.concatenate([m0, m0[-1:]])
    rk = vl.to_rate_k(m0); en = vl.frame_energy_db(vl.from_rate_k(m0, rk))
    mean = rk.mean(1, keepdims=True); sh = rk - mean
    shq = sh.copy(); shq[::2] = v.vq.quantize(sh[::2])
    shpf = vl.postfilter_rk(shq, 1.35)
    V = {}
    V['a_rk'] = vl.set_energy(vl.from_rate_k(m0, rk), en)
    V['b_rk_dec'] = vl.set_energy(vl.from_rate_k(m0, vl.decimate_interp(rk)), vl.decimate_interp(en))
    V['c_vq_dec'] = vl.set_energy(vl.from_rate_k(m0, vl.decimate_interp(shq)), vl.decimate_interp(en))
    V['d_vq_pf_dec'] = vl.set_energy(vl.from_rate_k(m0, vl.decimate_interp(shpf)), vl.decimate_interp(en))
    V['e_full'] = v.decode(v.encode(x))
    fr, PX = spec(x)
    hf = PX[fr > 2500].sum(0); lf = PX[fr < 1000].sum(0) + 1e-12; tot = PX.sum(0)
    sel = (hf/lf > 2) & (tot > np.percentile(tot, 90) * 10**-2.5)
    for k, mm in V.items():
        y = vl.synthesise(mm)[160:]; y = np.pad(y, (0, max(0, len(x)-len(y))))[:len(x)]
        _, PY = spec(y)
        S.setdefault(k, [0, 0])
        S[k][0] = S[k][0] + np.array([PX[(fr>=a)&(fr<b)][:,sel].sum() for a,b in zip(B[:-1],B[1:])])
        S[k][1] = S[k][1] + np.array([PY[(fr>=a)&(fr<b)][:,sel].sum() for a,b in zip(B[:-1],B[1:])])
for k,(o,d) in S.items():
    print(f'{k:12s}', ' '.join(f'{a}-{b}:{10*np.log10(dd/oo):+.1f}' for a,b,oo,dd in zip(B[:-1],B[1:],o,d)))
