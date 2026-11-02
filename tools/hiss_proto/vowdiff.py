import sys; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb
vl.TRACK2['lag']=8
B=[(3500,4000),(4000,5000),(5000,6000),(6000,7000),(7000,8000)]
for c in ['ears_p051_f','librispeech_8842_f','ears_p047_f','ears_p020_m']:
    x=sf.read(f'clips16/{c}.wav')[0]; x=x[:len(x)//320*320]
    s=hb.SottoV38(vowel='adaptive'); b=s.encode(x); m=s.decode(b)
    Po=np.abs(hb.stft(x))**2; Ho=hb.bands_db(Po,B)
    E,v,f,uu=hb.frame_map(m,b,len(Ho)); half=np.arange(len(Ho))>400   # after offsets settle
    L=hb.bands_db(Po,[(0,1000)])[:,0]; Hi=hb.bands_db(Po,[(2500,4000)])[:,0]; vow=(L-Hi>15)&(L>np.percentile(L,50))&half
    out=[]
    for vs,tr in (('template',0),('template',-4)):
        s.vowel_shape='template'; s.voiced_trim=tr; y=s.decode_audio16(b,n16=len(x)); Ha=hb.bands_db(np.abs(hb.stft(y))**2,B)[:len(Ho)]
        out.append(f'{vs} {tr}: V {(Ha-Ho)[(v==1)&half].mean(0).round(1)} vowels {(Ha-Ho)[vow].mean(0).round(1)}')
    print(c); [print('  ',o) for o in out]
