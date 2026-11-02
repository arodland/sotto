import sys; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, hiss_band as hb
D='/mnt/project-files/vocoder/listening/vowel'
for p in (2,3):
    o=sf.read(f'{D}/pair{p}_0_original.wav')[0]; L=hb.bands_db(np.abs(hb.stft(o))**2,[(300,3000)])[:,0]; live=(L>np.percentile(L,40))
    for v in ('0_original','1_A_as_heard','7_vowel_shaped','9_smooth_layer','10_smooth_layer_quieter'):
        y=sf.read(f'{D}/pair{p}_{v}.wav')[0]; Hy=hb.bands_db(np.abs(hb.stft(y))**2,[(4000,8000)])[:,0]
        d=np.abs(np.diff(Hy))[live[1:]&live[:-1]]
        print(f'pair{p} {v:24s} 4-8k frame-to-frame jumps: mean {d.mean():.1f} dB, >10 dB: {np.mean(d>10)*100:.0f}%')
