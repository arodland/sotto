"""Over-the-air ceiling: bit-exactness on normal voices, lock time, high band after lock."""
import os, sys, glob, numpy as np, soundfile as sf
from scipy.signal import butter, sosfiltfilt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling as cl, ceiling_air as ca
from measure import ENC, DEC, vl, hb
vl.TRACK2['lag'] = 8
C = os.environ.get('CLIPS16', '/tmp/claude-0/hiss/clips16')
def hf(y, x, fc, a):
    a = int(a * 16000); Y = np.abs(np.fft.rfft(y[a:]))**2; f = np.fft.rfftfreq(len(y) - a, 1/16000)
    return 10*np.log10(Y[(f > fc+300) & (f < 7900)].sum() / Y[(f > 300) & (f < 4000)].sum())
for c, lp in [('librispeech_777_m', None), ('librispeech_3170_m', None), ('librispeech_8842_f', 5000), ('librispeech_251_m', 6000),
              ('librispeech_652_m', 5000), ('librispeech_3081_f', 5500), ('librispeech_6313_f', None), ('librispeech_2428_m', None),
              ('ears_p020_x', None), ('ears_p095_x', None)]:
    x = sf.read(f'{C}/{c}.wav')[0]
    if lp: x = sosfiltfilt(butter(10, lp, fs=16000, output='sos'), x)
    x = x[:len(x)//320*320]
    b0 = hb.SottoV38(**ENC).encode(x)
    s = ca.SottoV38S(**ENC); b1 = s.encode(x)
    ceil = ca.SottoV38S(**DEC)._frame_ceiling(b1)
    first = next((g for g, v in enumerate(ceil) if v is not None), None)
    pc0 = hb.SottoV38(**DEC)._read_pc(b0, np.ones(len(b0), bool)); pc1 = hb.SottoV38(**DEC)._read_pc(b1, np.ones(len(b1), bool))
    lock = lambda pc: next((g for g in range(len(pc)) if np.any(pc[g] != 0)), None)
    row = f'{c + (" LP%d" % lp if lp else ""):24s} exact v3.8: {np.array_equal(b0, b1)!s:5s} ceiling {ceil[-1]} from {(first or 0)*0.02:.2f} s | vowel shape lock {lock(pc0) and lock(pc0)*0.02} -> {lock(pc1) and lock(pc1)*0.02} s'
    if first is not None:
        y0 = hb.SottoV38(**DEC).decode_audio16(b0, n16=len(x)); y1 = ca.SottoV38S(**DEC).decode_audio16(b1, n16=len(x))
        fc = ceil[-1][0]; a = first * 0.02
        row += f' | after lock, above fc: orig {hf(x, x, fc, a):+.0f} v3.8 {hf(y0, x, fc, a):+.0f} air {hf(y1, x, fc, a):+.0f} dB'
    print(row, flush=True)
