"""v3.8 round 2 page: original / v3.7 / v3.8 candidate (z fix in, shape over the air) on
voices Jeff has not heard yet, plus the fading LibriSpeech pair as the reference.
ONLY=<group,...> renders a subset; groups render independently so they can run in parallel."""
import sys, os; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto'); sys.path.insert(0,'/home/claude/sotto/tools/hiss_proto')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb, audio_race as ar, waveforms as wf, c4fm_sim as cs
from scipy.signal import resample_poly
from modem.link import Transmitter, Receiver
vl.TRACK2['lag']=8
S='/tmp/claude-0/-home-claude/929ea970-7bb5-552e-b050-589efb5f7fa7/scratchpad/v38r2/audio'; os.makedirs(S, exist_ok=True)
ONLY=os.environ.get('ONLY')
ENC=dict(vowel='speaker',whisper=True,vsmooth=True,sib_db=-10.0,sib_voiced=True)
DEC=dict(vowel='speaker',vowel_shape='fine',voiced_trim=-5,blend=True,release_ms=0,keep_mixed=True)
def channel(bits, cn0, fd, seed):
    mute=np.zeros(48,np.int8); n=len(bits)
    disc,_=wf.fm_channel(Transmitter('sotto_vw').modulate(bits),cn0,fd=fd,seed=seed)
    rx=Receiver('sotto_vw',mute=mute).receive(disc,t0=ar.LEAD+4800)
    fr=np.concatenate([rx['frames'],np.repeat(mute[None],max(0,n-len(rx['frames'])),0)])[:n]
    return fr, np.concatenate([rx['good'],np.zeros(max(0,n-len(rx['good'])),bool)])[:n]
C='/tmp/claude-0/hiss/clips16/'
SRC={'fading8842':('librispeech_8842_f',(52,cs.doppler(446e6,60),102),(4,10)),
     'p020':('ears_p020_m',None,(0,6)),'l2428':('librispeech_2428_m',None,(0,6)),'l251':('librispeech_251_m',None,(0,6)),
     'l3081':('librispeech_3081_f',None,(0,6)),'l3536':('librispeech_3536_f',None,(0,6)),'l6313':('librispeech_6313_f',None,(0,6)),
     'l652':('librispeech_652_m',None,(0,6)),'l777':('librispeech_777_m',None,(0,6)),
     'fading652':('librispeech_652_m',(52,cs.doppler(146e6,60),7),(0,6))}
for name,(key,ch,(a,e)) in SRC.items():
    if ONLY and name not in ONLY.split(','): continue
    x=sf.read(C+key+'.wav')[0]; x=x[:len(x)//320*320]
    v=vl.SottoV37(); b7=v.encode(resample_poly(x,1,2)); f7,g7=channel(b7,*ch) if ch else (b7,np.ones(len(b7),bool))
    y8=vl.synthesise(v.decode(f7,g7))[160:]; y8=np.pad(y8,(0,max(0,len(x)//2-len(y8))))[:len(x)//2]; y37=resample_poly(y8,2,1)
    b=hb.SottoV38(**ENC).encode(x); fr,gd=channel(b,*ch) if ch else (b,np.ones(len(b),bool))
    y38=hb.SottoV38(**DEC).decode_audio16(fr,gd,n16=len(x))
    A,E=int(a*16000),min(int(e*16000),len(x))
    def snip(y):
        y=y[A:E].copy(); y[:320]*=np.linspace(0,1,320); y[-320:]*=np.linspace(1,0,320)
        return y*np.sqrt(np.mean(x[A:E]**2)/np.mean(y**2))
    w=lambda n,y: sf.write(f'{S}/{name}_{n}.wav',np.round(np.clip(snip(y),-1,1)*32767).astype(np.int16),16000,subtype='PCM_16')
    w('0_original',x); w('1_v37',y37); w('2_v38',y38)
    print(name,'done',f'lost {int((~gd).sum())}' if ch else '',flush=True)
