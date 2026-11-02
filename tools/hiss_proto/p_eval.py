from proto import *
import audio_race as ar, sys
from multiprocessing import Pool
clips = sorted(glob.glob('/mnt/project-files/vocoder/samples-clean/*.flac')) + sorted(glob.glob('/mnt/project-files/vocoder/samples15/*.flac'))
W50 = 2*np.pi/160
cb5 = np.load('cb5.npy') if os.path.exists('cb5.npy') else None
V = [('v3.6', {}), ('uv50', dict(uv_wo=W50)), ('uv50+oracleU', dict(uv_wo=W50, oracle=True))]
if cb5 is not None: V.append(('uv50+cb5', dict(uv_wo=W50, cb5=cb5)))
def run(f):
    x, _ = sf.read(f); out = []
    for n, kw in V:
        p = Proto(**kw); out.append(ar.score(x, vl.synthesise(p.decode(p.encode(x))), 160))
    return out
if __name__ == '__main__':
    with Pool(4) as p: R = np.array(p.map(run, clips))
    for i, (n, _) in enumerate(V): print(f'{n:14s} PESQ {R[:, i, 0].mean():.3f} ESTOI {R[:, i, 1].mean():.3f}')
