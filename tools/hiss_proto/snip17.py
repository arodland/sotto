"""v3.8 page: original / v3.7 / v3.8 with the speaker shape measured from the original /
v3.8 with the shape over the side channel."""
import sys, os; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto'); sys.path.insert(0,'/home/claude/sotto/tools/hiss_proto')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb, audio_race as ar, waveforms as wf, c4fm_sim as cs, vowseg as vs
from scipy.signal import resample_poly
from modem.link import Transmitter, Receiver
vl.TRACK2['lag']=8
S='/tmp/claude-0/-home-claude/929ea970-7bb5-552e-b050-589efb5f7fa7/scratchpad/v38/audio'; os.makedirs(S, exist_ok=True)
ONLY=os.environ.get('ONLY')
V7=dict(whisper=True,vsmooth=True,sib_db=-10.0,sib_voiced=True)   # v3.8 encoder as proposed
DEC=dict(vowel_shape='fine',voiced_trim=-5,blend=True,release_ms=0,keep_mixed=True)
def channel(bits, cn0, fd, seed):
    mute=np.zeros(48,np.int8); n=len(bits)
    disc,_=wf.fm_channel(Transmitter('sotto_vw').modulate(bits),cn0,fd=fd,seed=seed)
    rx=Receiver('sotto_vw',mute=mute).receive(disc,t0=ar.LEAD+4800)
    fr=np.concatenate([rx['frames'],np.repeat(mute[None],max(0,n-len(rx['frames'])),0)])[:n]
    return fr, np.concatenate([rx['good'],np.zeros(max(0,n-len(rx['good'])),bool)])[:n]
jx=resample_poly(sf.read('/tmp/claude-0/jeff/vowelvoice.flac')[0],1,3); jx=0.5*jx/np.abs(resample_poly(jx,1,2)).max()
wx=vs.load16('/tmp/claude-0/wife/rec.wav'); wx=0.5*wx/np.abs(wx).max()
px=vs.load16('/tmp/claude-0/jeffphone/rec.wav'); px=0.5*px/np.abs(px).max()
C='/tmp/claude-0/hiss/clips16/'
SRC={'wifewhisper':('wife',wx,None,(9.4,15.0)),'jeffwhisper':('jeff',jx,None,(2.0,9.0)),'jeffphonewhisper':('jeffphone',px,None,(5.0,11.3)),
     'wife':('wife',wx,None,(15.2,22.8)),'jeffphone':('jeffphone',px,None,(11.8,15.4)),
     'pair2':('8842',sf.read(C+'librispeech_8842_f.wav')[0],(52,cs.doppler(446e6,60),102),(4,10)),
     'pair3':('p051',sf.read(C+'ears_p051_f.wav')[0],None,(4,10)),
     'p095':('p095',sf.read(C+'ears_p095_m.wav')[0],None,(0,6)),
     'p047':('p047',sf.read(C+'ears_p047_f.wav')[0],None,(0,6))}
EXTRA={}
cache={}
for name,(key,x,ch,(a,e)) in SRC.items():
    if ONLY and name not in ONLY.split(','): continue
    x=x[:len(x)//320*320]; nfr=(len(x)-hb.N)//hb.H+1
    if key not in cache:
        r={}
        v=vl.SottoV37(); b7=v.encode(resample_poly(x,1,2)); f7,g7=channel(b7,*ch) if ch else (b7,np.ones(len(b7),bool))
        y8=vl.synthesise(v.decode(f7,g7))[160:]; y8=np.pad(y8,(0,max(0,len(x)//2-len(y8))))[:len(x)//2]; r['v37']=resample_poly(y8,2,1)
        b=hb.SottoV38(**V7).encode(x); fr,gd=channel(b,*ch) if ch else (b,np.ones(len(b),bool))
        sp=hb.fine_speaker(x, hb.SottoV38().decode_audio16(fr,gd,n16=len(x)), nfr); sp=np.round(np.clip(sp,-30,30)/4)*4
        r['v38m']=hb.SottoV38(spk_pc=sp,**DEC).decode_audio16(fr,gd,n16=len(x))
        b=hb.SottoV38(vowel='speaker',**V7).encode(x); fr,gd=channel(b,*ch) if ch else (b,np.ones(len(b),bool))
        r['v38']=hb.SottoV38(vowel='speaker',**DEC).decode_audio16(fr,gd,n16=len(x))
        win=hb.SottoV38(vowel='speaker')._read_pc(fr,gd); ch1=np.where((np.abs(win)>0).any(1))[0]; print(name,'measured pc',sp,'side channel first lands at',f'{ch1[0]*0.02:.2f}s' if len(ch1) else 'never','final',win[-1].tolist(),flush=True)
        cache[key]=r
    r=cache[key]; A,E=int(a*16000),min(int(e*16000),len(x))
    def snip(y):
        y=y[A:E].copy(); y[:320]*=np.linspace(0,1,320); y[-320:]*=np.linspace(1,0,320)
        return y*np.sqrt(np.mean(x[A:E]**2)/np.mean(y**2))
    w=lambda n,y: sf.write(f'{S}/{name}_{n}.wav',np.round(np.clip(snip(y),-1,1)*32767).astype(np.int16),16000,subtype='PCM_16')
    w('0_original',x); w('1_v37',r['v37']); w('2_v38_measured',r['v38m']); w('3_v38',r['v38'])
    print(name,'done',flush=True)
