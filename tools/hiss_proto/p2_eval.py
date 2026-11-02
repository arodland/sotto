from proto import *
import audio_race as ar
from multiprocessing import Pool
vl.TRACK2["lag"] = 8
clips = sorted(glob.glob('/mnt/project-files/vocoder/samples-clean/*.flac')) + sorted(glob.glob('/mnt/project-files/vocoder/samples15/*.flac'))
W = 2*np.pi/160
V = [('v3.7', None), ('level', dict(uv_wo=W, uv_gain=3.0)), ('level+gate', dict(uv_wo=W, uv_gain=3.0, tilt_gate=True)),
     ('level+onsetM', dict(uv_wo=W, uv_gain=3.0, onset_m=True)), ('level+gate+onsetM', dict(uv_wo=W, uv_gain=3.0, tilt_gate=True, onset_m=True))]
def run(f):
    x, _ = sf.read(f); out = []
    for n, kw in V:
        p = vl.SottoV37() if kw is None else Proto2(**kw)
        b = p.encode(x); out.append(ar.score(x, vl.synthesise(p.decode(b)), 160) + (len(getattr(p, 'recoded', [])) / len(b),))
    return out
if __name__ == '__main__':
    with Pool(4) as p: R = np.array(p.map(run, clips))
    for i, (n, _) in enumerate(V): print(f'{n:18s} PESQ {R[:, i, 0].mean():.3f} ESTOI {R[:, i, 1].mean():.3f} recoded {100*R[:, i, 2].mean():.1f}% of frames')
