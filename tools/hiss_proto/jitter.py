import sys; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, hiss_band as hb
D='/mnt/project-files/vocoder/listening/vowel'
for p in (2,3):
    o=sf.read(f'{D}/pair{p}_0_original.wav')[0]; a=sf.read(f'{D}/pair{p}_'+sys.argv[1]+'.wav')[0]
    Ho=hb.bands_db(np.abs(hb.stft(o))**2,hb.HB); Ha=hb.bands_db(np.abs(hb.stft(a))**2,hb.HB)
    Lo=hb.bands_db(np.abs(hb.stft(o))**2,[(300,3000)])[:,0]
    live=Lo>np.percentile(Lo,40)
    d=lambda X: np.abs(np.diff(X,axis=0))[live[1:]&live[:-1]].mean(0)
    print(f'pair{p} mean |frame-to-frame change| dB per band  orig', d(Ho).round(1), ' A', d(Ha).round(1))
    # within-frame temporal modulation: 2 ms envelope of 4-8k band, std/mean in voiced frames
    from scipy.signal import butter, sosfilt
    sos=butter(6,[4200,7800],'band',fs=16000,output='sos')
    for nm,x in (('orig',o),('A',a)):
        e=np.sqrt(np.convolve(sosfilt(sos,x)**2,np.ones(32)/32,'same'))
        fr=e[:len(e)//160*160].reshape(-1,160); n=min(len(fr),len(live)); fr=fr[:n]; m=live[:n]
        print(f'   {nm} within-10ms envelope modulation (std/mean): {np.mean(fr[m].std(1)/(fr[m].mean(1)+1e-9)):.2f}')
