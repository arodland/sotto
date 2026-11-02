import sys, os; sys.path.insert(0, '/home/claude/sotto/sim'); sys.path.insert(0, '/home/claude/sotto')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb, audio_race as ar, waveforms as wf, c4fm_sim as cs
from modem.link import Transmitter, Receiver
vl.TRACK2['lag'] = 8
D = '/mnt/project-files/vocoder/listening/vowel'; os.makedirs(D, exist_ok=True)
def channel(bits, cn0, fd, seed):
    mute = np.zeros(48, np.int8); n = len(bits)
    disc, _ = wf.fm_channel(Transmitter('sotto_vw').modulate(bits), cn0, fd=fd, seed=seed)
    rx = Receiver('sotto_vw', mute=mute).receive(disc, t0=ar.LEAD + 4800)
    fr = np.concatenate([rx['frames'], np.repeat(mute[None], max(0, n - len(rx['frames'])), 0)])[:n]
    return fr, np.concatenate([rx['good'], np.zeros(max(0, n - len(rx['good'])), bool)])[:n]
V = [('9_smooth_layer', -4, 'noise', 1.0), ('10_smooth_layer_quieter', -8, 'noise', 1.0)]
for pair, c, ch in [(2, 'librispeech_8842_f', (52, cs.doppler(446e6, 60), 102)), (3, 'ears_p051_f', None)]:
    x = sf.read(f'/tmp/claude-0/hiss/clips16/{c}.wav')[0]; x = x[:len(x)//320*320]
    b = hb.SottoV38().encode(x)
    fr, gd = channel(b, *ch) if ch else (b, np.ones(len(b), bool))
    a, e = 4 * 16000, 10 * 16000
    def snip(y):
        y = y[a:e].copy(); y[:320] *= np.linspace(0, 1, 320); y[-320:] *= np.linspace(1, 0, 320)
        return y * np.sqrt(np.mean(x[a:e] ** 2) / np.mean(y ** 2))
    sf.write(f'{D}/pair{pair}_0_original.wav', snip(x), 16000, subtype='PCM_16')
    for n, tr, tx, pu in V:
        y = hb.SottoV38(voiced_trim=tr, texture=tx, pulse=pu, vowel_shape='smooth').decode_audio16(fr, gd, n16=len(x))
        sf.write(f'{D}/pair{pair}_{n}.wav', snip(y), 16000, subtype='PCM_16')
print(sorted(os.listdir(D)))
