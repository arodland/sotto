"""Encode the 12 test clips + bench clips once with the v3.8 candidate (version 7) encoder
settings and cache bits + the decoder's own 16 kHz low band, for the follow-up scripts."""
import sys, os; sys.path.insert(0,'/home/claude/sotto/sim'); sys.path.insert(0,'/home/claude/sotto/tools/hiss_proto')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb, vowseg as vs, glob
from scipy.signal import resample_poly
vl.TRACK2['lag']=8
V7=dict(whisper=True,vsmooth=True,sib_db=3.0)
def sources():
    S={os.path.basename(c)[:-4]:sf.read(c)[0] for c in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav'))}
    jx=resample_poly(sf.read('/tmp/claude-0/jeff/vowelvoice.flac')[0],1,3); S['jeffATR']=0.5*jx/np.abs(resample_poly(jx,1,2)).max()
    wx=vs.load16('/tmp/claude-0/wife/rec.wav'); S['wife']=0.5*wx/np.abs(wx).max()
    px=vs.load16('/tmp/claude-0/jeffphone/rec.wav'); S['jeffphone']=0.5*px/np.abs(px).max()
    return {k:x[:len(x)//320*320] for k,x in S.items()}
if __name__=='__main__':
    only=sys.argv[1:]
    for k,x in sources().items():
        if only and k not in only: continue
        f=f'/tmp/claude-0/hiss/bits/{k}.npz'
        if os.path.exists(f): continue
        e=hb.SottoV38(vowel='speaker',**V7); b,hbands,_=e.encode_low(x)
        y16=e._low16(b,len(x))
        np.savez(f,bits=b,hb=hbands,y16=y16.astype(np.float32)); print(k,'cached',flush=True)
