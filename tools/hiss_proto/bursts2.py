from hiss import *
import audio_race as ar
from multiprocessing import Pool
clips = sorted(glob.glob('clips16/*.wav'))
V = [('hiss7', 0, None), ('hiss7m', 0, -3.0), ('hiss7m0', 0, 0.0), ('hiss7b', 12, None), ('hiss7bm', 12, -3.0)]
def run(c):
    x = sf.read(c)[0]; x = x[:len(x)//320*320]; x8 = resample_poly(x, 1, 2)
    PX = np.abs(stft(x))**2; hbx = bands_db(PX, [(4000, 8000)])[:, 0]; lbx = bands_db(PX, [(0, 3900)])[:, 0]
    ev = np.where((hbx[1:] - hbx[:-1] > 12) & (hbx[1:] > lbx[1:] - 15))[0] + 1
    fric = (bands_db(PX, [(2500, 3900)])[:, 0] - bands_db(PX, [(0, 1000)])[:, 0]) > 3
    out = []
    for n, br, mh in V:
        h = Hiss('hiss_model.npz', burst=br); b = h.encode(x); y = h.decode(b, mode='hiss7', n16=len(x), mixed_hb=mh)
        k = min(len(y), len(x)); PY = np.abs(stft(y[:k]))**2; hby = bands_db(PY, [(4000, 8000)])[:, 0]
        ee = np.concatenate([np.arange(t, t + 3) for t in ev]) if len(ev) else np.array([], int); ee = ee[ee < len(hby)]
        y8 = resample_poly(y, 1, 2)
        nb = ar.score(x8[:len(y8)], np.concatenate([np.zeros(160), y8]), 160)[0]
        allv = (hbx[:len(hby)] > np.percentile(hbx, 40))
        out.append((np.mean(hby[ee] - hbx[ee]) if len(ee) else 0, np.sqrt(np.mean((hby[allv] - hbx[:len(hby)][allv])**2)), nb, len(getattr(h, 'forced', []))))
    return out
with Pool(4) as p: R = np.array(p.map(run, clips))
print('variant   burst HB shortfall dB | HB rms err all live frames | NB PESQ | forced frames/clip')
for i, (n, _, _) in enumerate(V): print(f'{n:9s} {R[:, i, 0].mean():6.1f} | {R[:, i, 1].mean():5.1f} | {R[:, i, 2].mean():.3f} | {R[:, i, 3].mean():.1f}')
