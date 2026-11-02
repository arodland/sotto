import sys, os; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb, audio_race as ar, waveforms as wf, c4fm_sim as cs
from scipy.signal import resample_poly
from modem.link import Transmitter, Receiver
from peakest import peak_of
vl.TRACK2['lag']=8
S='/tmp/claude-0/-home-claude/929ea970-7bb5-552e-b050-589efb5f7fa7/scratchpad/whistle/audio'
TRIM=float(os.environ.get('FTRIM','0')); ONLY=os.environ.get('ONLY'); os.makedirs(S, exist_ok=True)
def channel(bits, cn0, fd, seed):
    mute=np.zeros(48,np.int8); n=len(bits)
    disc,_=wf.fm_channel(Transmitter('sotto_vw').modulate(bits),cn0,fd=fd,seed=seed)
    rx=Receiver('sotto_vw',mute=mute).receive(disc,t0=ar.LEAD+4800)
    fr=np.concatenate([rx['frames'],np.repeat(mute[None],max(0,n-len(rx['frames'])),0)])[:n]
    return fr, np.concatenate([rx['good'],np.zeros(max(0,n-len(rx['good'])),bool)])[:n]
jx=resample_poly(sf.read('/tmp/claude-0/jeff/vowelvoice.flac')[0],1,3); jx=0.5*jx/np.abs(resample_poly(jx,1,2)).max(); jx=jx[:len(jx)//320*320]
# Cache key is the source name: id() of a sliced temp array can be reused after it is freed,
# which once made pair2/pair3 decode Jeff's recording.
wx=__import__('vowseg').load16('/tmp/claude-0/wife/rec.wav'); wx=0.5*wx/np.abs(wx).max(); wx=wx[:len(wx)//320*320]
SRC={'wife':('wife',wx,None,(15.2,22.8)),'wifewhisper':('wife',wx,None,(9.4,15.0)),'jeff':('jeff',jx,None,(9.4,17.0)),'jeffwhisper':('jeff',jx,None,(2.0,9.0)),'pair2':('8842',sf.read('clips16/librispeech_8842_f.wav')[0],(52,cs.doppler(446e6,60),102),(4,10)),'pair3':('p051',sf.read('clips16/ears_p051_f.wav')[0],None,(4,10))}
cache={}
for name,(key,x,ch,(a,e)) in SRC.items():
    if ONLY and name not in ONLY.split(','): continue
    x=x[:len(x)//320*320]; pf,pdb=peak_of(x)
    if key not in cache:
        b=hb.SottoV38().encode(x); fr,gd=channel(b,*ch) if ch else (b,np.ones(len(b),bool))
        v=vl.SottoV37(); b7=v.encode(resample_poly(x,1,2)); f7,g7=channel(b7,*ch) if ch else (b7,np.ones(len(b7),bool))
        y8=vl.synthesise(v.decode(f7,g7))[160:]; y8=np.pad(y8,(0,max(0,len(x)//2-len(y8))))[:len(x)//2]
        cache[key]=(fr,gd,resample_poly(y8,2,1))
    fr,gd,y37=cache[key]
    A,E=int(a*16000),int(e*16000)
    def snip(y):
        y=y[A:E].copy(); y[:320]*=np.linspace(0,1,320); y[-320:]*=np.linspace(1,0,320)
        return y*np.sqrt(np.mean(x[A:E]**2)/np.mean(y**2))
    w=lambda n,y: sf.write(f'{S}/{name}_{n}.wav',np.round(np.clip(snip(y),-1,1)*32767).astype(np.int16),16000,subtype='PCM_16')
    w('0_original',x); w('1_v37',y37)
    w('2_consonants_only',hb.SottoV38().decode_audio16(fr,gd,n16=len(x)))
    w('3_flat_layer',hb.SottoV38(vowel_shape='smooth',voiced_trim=-8).decode_audio16(fr,gd,n16=len(x)))
    w('4_whistle',hb.SottoV38(vowel_shape='smooth',voiced_trim=-8,peak_hz=pf,peak_db=min(max(pdb,4),12)).decode_audio16(fr,gd,n16=len(x)))
    sp=hb.fine_speaker(x, hb.SottoV38().decode_audio16(fr,gd,n16=len(x)), (len(x)-hb.N)//hb.H+1); sp=np.round(np.clip(sp,-30,30)/4)*4   # 4 dB steps ~ 4 bits each
    y5=hb.SottoV38(vowel_shape='fine',voiced_trim=TRIM,spk_pc=sp).decode_audio16(fr,gd,n16=len(x)); w('5_vowel_following',y5)
    y3=hb.SottoV38(vowel_shape='smooth',voiced_trim=-8).decode_audio16(fr,gd,n16=len(x))
    P0=np.abs(hb.stft(x))**2; lo=hb.bands_db(P0,[(0,1000)])[:,0]; tot=10*np.log10(P0.sum(1)+1e-12); vow=(lo-hb.bands_db(P0,[(2500,3000)])[:,0]>10)&(tot>np.percentile(tot,60))
    hbd=lambda y: hb.bands_db(np.abs(hb.stft(y))**2,[(4000,8000)])[:,0]
    n=min(len(vow),len(hbd(y5))); vv=vow[:n]
    print(name,'peak',pf,round(pdb,1),'pc',sp,'HB in vowels: orig %.1f flat-8 %.1f fine %.1f'%(hbd(x)[:n][vv].mean(),hbd(y3)[:n][vv].mean(),hbd(y5)[:n][vv].mean()))
