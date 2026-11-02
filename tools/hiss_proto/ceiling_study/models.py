"""Representation error of hiss-envelope models on sibilant frames (unquantised, true levels).
Error: rms dB over 250 Hz bins 4-8 kHz, each frame floored 40 dB under its peak bin."""
import os, sys, glob, numpy as np, soundfile as sf
from scipy.signal import butter, sosfiltfilt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import ceiling as cl; hb = cl.hb
FC = cl.FC
def bands(P, edges):
    B = list(zip(edges[:-1], edges[1:])); return hb.bands_db(P, B), (edges[:-1]+edges[1:])/2
def model(P, nb, ceil, warp, depth=60.0, slope=None):
    """nb band levels; warp: bands span 4 kHz..fc instead of 4..8; ceiling: brick wall of `depth` dB
    (or the measured slope) above fc. Returns 250 Hz-bin dB envelope per frame (before floors)."""
    fc = ceil[0] if ceil else 8000
    top = fc if (warp and ceil) else 8000
    L, c = bands(P, np.linspace(4000, top, nb+1))
    env = np.stack([np.interp(FC, c, l) for l in L])
    if ceil:
        S = slope if slope is not None else ceil[1]
        env += -np.clip((FC - fc) * S / 1000, 0, depth)
    return env
def err(T, M):
    fl = T.max(1, keepdims=True) - 40
    # compare shapes level-free: match power in the passband via common offset (levels are coded anyway)
    e = np.maximum(M, fl) - np.maximum(T, fl)
    return np.sqrt((e**2).mean())
clips = [('librispeech_777_m', None), ('librispeech_3170_m', None), ('librispeech_8842_f', 5000), ('librispeech_251_m', 6000),
         ('librispeech_652_m', 5000), ('ears_p047_x', 5500), ('librispeech_6313_f', None), ('librispeech_251_m', None), ('librispeech_8842_f', None), ('ears_p020_x', None)]
V = {'4 fixed (v3.8)': dict(nb=4, c=False, warp=False), '3 fixed': dict(nb=3, c=False, warp=False),
     '4 fixed + ceiling': dict(nb=4, c=True, warp=False), '3 fixed + ceiling': dict(nb=3, c=True, warp=False),
     '3 warped + hard ceiling': dict(nb=3, c=True, warp=True, slope=1000.0), '4 warped + hard ceiling': dict(nb=4, c=True, warp=True, slope=1000.0),
     '3 warped + measured slope': dict(nb=3, c=True, warp=True)}
print(f'{"clip":28s} ceiling      ' + ' | '.join(f'{k:>12.12s}' for k in V))
for c, lp in clips:
    x = sf.read(f'/tmp/claude-0/hiss/clips16/{c}.wav')[0]
    if lp: x = sosfiltfilt(butter(10, lp, fs=16000, output='sos'), x)
    ceil = cl.measure(x)
    P = np.abs(hb.stft(x))**2; s = cl.sib_frames(P); T = cl.fine(P[s])
    r = [err(T, model(P[s], v['nb'], ceil if v['c'] else None, v['warp'], slope=v.get('slope'))) for v in V.values()]
    print(f'{c+(" LP"+str(lp) if lp else ""):28s} {str(ceil and (int(ceil[0]),int(ceil[1]))):12s} n={s.sum():3d} ' + ' | '.join(f'{v:12.1f}' for v in r))
