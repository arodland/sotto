"""Cost of dropping the 4th shape stage (8 bits) in UU frames: NB-PESQ on the test clips."""
import sys, glob, os, copy, numpy as np, soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from measure import run, vl, hb, DEC
from scipy.signal import resample_poly
from pesq import pesq
vl.TRACK2['lag'] = 8
d = hb.SottoV38(**DEC); vq = d.vq
Z = int(np.argmin((vq.cb[3] ** 2).sum(1))); print('zero-ish codeword', Z, 'norm', np.sqrt((vq.cb[3][Z]**2).sum()).round(2), 'mean norm', np.sqrt((vq.cb[3]**2).sum(1)).mean().round(2))
d2 = hb.SottoV38(**DEC); d2.vq = copy.deepcopy(vq); d2.vq.cb[3][Z] = 0; d2.sq.vq = d2.vq
r = []
for p in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    x, b, y = run(p); x8 = resample_poly(x, 1, 2)
    b2 = b.copy(); uu = (b[:, 0] == 0) & (b[:, 1] == 0)
    b2[uu, 40:48] = [(Z >> (7 - i)) & 1 for i in range(8)]
    s = []
    for dd, bb in ((d, b), (d2, b2)):
        y8 = vl.synthesise(dd.decode(bb))[vl.ar.CODEC_DELAY:]; n = min(len(y8), len(x8)); s.append(pesq(8000, x8[:n], y8[:n], 'nb'))
    r.append(s); print(os.path.basename(p)[:-4].ljust(22), 'UU %.0f%%' % (100*uu.mean()), 'NB-PESQ %.3f -> %.3f' % tuple(s), flush=True)
print('mean %.3f -> %.3f' % tuple(np.mean(r, 0)))
