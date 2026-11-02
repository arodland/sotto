"""Training frames for a 16-bin s codebook: LibriSpeech dev-clean speakers not in the test set.
Per STFT frame of UU codec frames: true 250 Hz bins 4-8 kHz (dB), the decoder's 4-band guess,
the codec frame number, and a sibilant flag."""
import sys, os, glob, numpy as np, soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling as cl; from measure import ENC, vl, hb
vl.TRACK2['lag'] = 8
LS = '/tmp/claude-0/data/ls/LibriSpeech/dev-clean'
TEST = {'2428', '251', '3081', '3536', '6313', '652', '777', '8842'}
spk = [s for s in sorted(os.listdir(LS), key=int) if s not in TEST]
part = int(sys.argv[1]); nparts = int(sys.argv[2])
out = dict(T=[], p=[], sib=[], spk=[])
for s in spk[part::nparts]:
    for f in sorted(glob.glob(f'{LS}/{s}/*/*.flac'))[:6]:
        x = sf.read(f)[0]; x = x[:len(x)//320*320]
        if len(x) < 32000: continue
        e = hb.SottoV38(**ENC)
        bits, hbands, m = e.encode_low(x)
        P = np.abs(hb.stft(x))**2
        E, v, fr, uu = hb.frame_map(m, bits, len(P))
        p = hb.guess(e.W, E, v)
        sib = cl.sib_frames(P)
        T = cl.fine(P)
        out['T'].append(T[uu]); out['p'].append(p[uu]); out['sib'].append(sib[uu]); out['spk'].append(np.full(uu.sum(), int(s)))
    print(s, flush=True)
np.savez(f'/tmp/claude-0/sib/train_{part}.npz', **{k: np.concatenate(v) for k, v in out.items()})
