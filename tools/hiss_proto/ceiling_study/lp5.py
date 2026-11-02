"""Does v3.8 follow a speaker whose hiss stops at 5 or 6 kHz? Low-pass a clip steeply, code it, compare."""
import os, sys, numpy as np, soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from measure import *
from scipy.signal import butter, sosfiltfilt
FB2 = np.arange(3000, 8000, 250)
for clip in ('librispeech_8842_f', 'librispeech_251_m'):
    x0 = sf.read(f'/tmp/claude-0/hiss/clips16/{clip}.wav')[0]
    for fc in (None, 5000, 6000):
        x = x0 if fc is None else sosfiltfilt(butter(10, fc, fs=16000, output='sos'), x0)
        sf.write(f'/tmp/claude-0/sib/lp_{clip}_{fc}.wav', x, 16000, subtype='FLOAT')
        _, b, y = run(f'/tmp/claude-0/sib/lp_{clip}_{fc}.wav')
        x = x[:len(x)//320*320]; P, s = sib_mask(x, b)
        Q = np.abs(hb.stft(y[:len(x)]))**2; n = min(len(P), len(Q))
        O = 10*np.log10(P[:n][s[:n]].mean(0)+1e-12); D = 10*np.log10(Q[:n][s[:n]].mean(0)+1e-12)
        ref = O[(hb.FR>=4000)&(hb.FR<4500)].mean()
        bb = lambda Z: ' '.join(f'{Z[(hb.FR>=a)&(hb.FR<a+250)].mean()-ref:+4.0f}' for a in FB2[::2])
        print(f'{clip} LP {fc}: n={s[:n].sum()}\n   orig {bb(O)}\n   v3.8 {bb(D)}')
print('kHz      ' + ' '.join(f'{a/1000:4.1f}' for a in FB2[::2]))
