from common import *
import audio_race as ar
from multiprocessing import Pool
clips = sorted(glob.glob('/mnt/project-files/vocoder/samples-clean/*.flac')) + sorted(glob.glob('/mnt/project-files/vocoder/samples15/*.flac'))
def setuv(m, wo):
    m = m.copy()
    for k in range(len(m)):
        if m[k]['voiced'] == 0:
            m[k]['Wo'] = wo; m[k]['L'] = int(np.floor(np.pi / wo))
    return m
V = [('base', None, 0), ('rebuild_same', -1, 0), ('uv100', 2*np.pi/80, 0), ('uv50', 2*np.pi/160, 0), ('uv50+1.5dB', 2*np.pi/160, 1.5), ('uv50+3dB', 2*np.pi/160, 3)]
def run(f):
    x, _ = sf.read(f); v = vl.SottoV36(); m0 = v.decode(v.encode(x)); out = []
    for name, wo, g in V:
        # amplitudes must be re-sampled from the envelope at the new spacing: rebuild via rate-K
        m = m0
        if wo is not None:
            rk = vl.to_rate_k(m0); en = vl.frame_energy_db(m0)
            m = m0.copy() if wo == -1 else setuv(m0, wo); keep = m0['voiced'] != 0
            mm = vl.set_energy(vl.from_rate_k(m, rk), en)
            mm[keep] = m0[keep]; m = mm
        m = level_fix(m, g, 0)
        out.append(ar.score(x, vl.synthesise(m), 160))
    return out
with Pool(4) as p: R = np.array(p.map(run, clips))
for i, (n, _, _) in enumerate(V): print(f'{n:12s} PESQ {R[:, i, 0].mean():.3f} ESTOI {R[:, i, 1].mean():.3f}')
