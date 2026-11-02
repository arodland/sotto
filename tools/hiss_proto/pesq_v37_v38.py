"""PESQ-NB and ESTOI (0-4 kHz core) of v3.7 vs the v3.8 candidate on the 12 test clips."""
import sys, glob, os; sys.path.insert(0,'/home/claude/sotto/sim')
import numpy as np, soundfile as sf, vocoder_lab as vl, hiss_band as hb
from scipy.signal import resample_poly
from pesq import pesq
from pystoi import stoi
vl.TRACK2['lag']=8
rows=[]
for c in sorted(glob.glob('/tmp/claude-0/hiss/clips16/*.wav')):
    k=os.path.basename(c)[:-4]; x=sf.read(c)[0]; x=x[:len(x)//320*320]; x8=resample_poly(x,1,2)
    v=vl.SottoV37(); b7=v.encode(x8); y7=vl.synthesise(v.decode(b7))[vl.ar.CODEC_DELAY:]
    b8=np.load(f'/tmp/claude-0/hiss/bits/{k}.npz')['bits']; y8=vl.synthesise(hb.SottoV38().decode(b8))[vl.ar.CODEC_DELAY:]
    r=[]
    for y in (y7,y8):
        n=min(len(y),len(x8)); r+= [pesq(8000,x8[:n],y[:n],'nb'), stoi(x8[:n],y[:n],8000,extended=True)]
    rows.append(r); print(f"{k:22s} v3.7 {r[0]:.3f}/{r[1]:.3f}  v3.8 {r[2]:.3f}/{r[3]:.3f}",flush=True)
m=np.mean(rows,0); print(f"mean                   v3.7 {m[0]:.3f}/{m[1]:.3f}  v3.8 {m[2]:.3f}/{m[3]:.3f}")
