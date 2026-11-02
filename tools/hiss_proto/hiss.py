"""Prototype v3.7 candidates on top of SottoV36 (realtime tracker, lag 8):
  P1  'level'  : unvoiced halves synthesised with 50 Hz noise spacing, +UVG dB (zero bits)
  P3  'hiss7'  : P1 + 4-8 kHz noise in all-unvoiced frames, levels from a 7-bit index sent in
                 the (now unused) pitch field of those frames; 16 kHz output
  P2  'hiss0'  : P1 + 4-8 kHz noise in unvoiced halves at a level guessed by the decoder (no bits)
"""
from bwe import *
from proto import Proto, Proto2
import pickle
vl.TRACK2["lag"] = 8
W50 = 2 * np.pi / 160
UVG = 3.0
HB_LAG = 2

def conceal_runs(good):
    run = np.zeros(len(good), int); r = 0
    for f, g in enumerate(good):
        r = 0 if g else r + 1; run[f] = r
    return run

def conceal_db(run, half):
    if run == 0: return 0.0
    if run > 4: return -80.0
    return -6.0 * max(0, run - 2 + half / 2)

class Hiss:
    def __init__(self, model=None, burst=0, fix2=False):
        self.codec = Proto2(uv_wo=W50, uv_gain=UVG, tilt_gate=True, onset_m=(fix2 == 'onset')) if fix2 else Proto(uv_wo=W50)
        self.burst, self.fix2 = burst, fix2
        if model:
            z = np.load(model, allow_pickle=True)
            self.bwe = BWE(); self.bwe.W = z['W'].item(); self.cb = z['cb']

    def _decode_model(self, bits, good=None):
        return self.codec.decode(bits, good)

    def _dstruct(self, bits, m, n16):
        nfr = (n16 - N) // H + 1
        d = dict(m=m, bits=bits, X=np.zeros((nfr, 1)))
        attach_voicing(d, HB_LAG); d['E'] = model_feats(d, HB_LAG)
        return d

    def encode(self, x16, side=True):
        x8 = resample_poly(x16, 1, 2)
        bits = self.codec.encode(x8)
        if not side:
            return bits
        P16 = np.abs(stft(x16)) ** 2
        hb = bands_db(P16, HB)
        if self.burst:                                 # stop bursts/aspiration: send as UU
            m = self._decode_model(bits); d = self._dstruct(bits, m, len(x16))
            hbt = bands_db(P16, [(4000, 8000)])[:, 0]; lbt = bands_db(P16, [(0, 3900)])[:, 0]
            ev = np.where((hbt[1:] - hbt[:-1] > self.burst) & (hbt[1:] > lbt[1:] - 15))[0] + 1
            self.forced = []
            for t in ev:
                if t >= len(d['frame']): continue
                f = d['frame'][t]
                if bits[f, 0] or bits[f, 1]:
                    bits[f, 0] = bits[f, 1] = 0; self.forced.append(f)
        m = self._decode_model(bits)                   # the encoder's local decoder
        d = self._dstruct(bits, m, len(x16))
        d['hb'] = hb[:len(d['E'])]
        p = self.bwe.predict(d)
        for f, r in frame_resid(d, p).items():
            i = int(np.argmin(((self.cb - r) ** 2).sum(1)))
            bits[f, 2:9] = [(i >> (6 - b)) & 1 for b in range(7)]
        for f in range(len(bits)):                     # UU frames with no STFT frame: index 0
            if not (bits[f, 0] or bits[f, 1]) and f not in frame_resid(d, p):
                bits[f, 2:9] = 0
        return bits

    def decode(self, bits, good=None, mode='hiss7', uvg=UVG, n16=None, mixed_hb=None):
        """-> 16 kHz output aligned with the input (codec delay removed)."""
        good = np.ones(len(bits), bool) if good is None else np.asarray(good)
        b2 = bits.copy(); last = None
        for f in range(len(b2)):                       # what concealment decodes
            if good[f]: last = b2[f].copy()
            elif last is not None: b2[f] = last
        m = self._decode_model(bits, good)
        md = m if self.fix2 else level_fix(m, uvg, 0)     # fix2: the codec applies the gated gain
        y8 = vl.synthesise(md)[160:]
        n8 = n16 // 2 if n16 else len(y8)
        y8 = np.pad(y8, (0, max(0, n8 - len(y8))))[:n8]
        y16 = resample_poly(y8, 2, 1)
        if mode in ('level', 'v37'):
            return y16
        d = self._dstruct(b2, m, len(y16)); d['y16'] = y16
        p = self.bwe.predict(d)
        run = conceal_runs(good)
        g = np.array([conceal_db(run[min(f, len(run) - 1)], 0) for f in d['frame']])
        if mode == 'hiss7':
            code = b2[:, 0] * 2 + b2[:, 1]
            mask = np.zeros(len(p))
            for t in range(len(p)):
                f = d['frame'][t]
                if d['uu'][t]: src = f
                elif d['v'][t] == 0:                   # unvoiced half of an edge frame:
                    src = next((g for g in (f - 1, f + 1) # borrow a neighbouring UU index
                                if 0 <= g < len(code) and code[g] == 0), None)
                else: src = None
                if src is not None:
                    p[t] += self.cb[int(''.join(map(str, b2[src, 2:9])), 2)]; mask[t] = 1
                elif mixed_hb is not None and d['v'][t] == 2:
                    p[t] += mixed_hb; mask[t] = 1        # decoder guess in mixed frames
        else:                                          # hiss0: decoder guess, 3 dB down
            p -= 3.0; mask = (d['v'] == 0).astype(float)
        p += g[:, None] + uvg                          # same level step as the narrowband
        return y16 + synth_hb(d, p, mask)

def train(out='hiss_model.npz'):
    from multiprocessing import Pool
    D = '/tmp/claude-0/data/ls/LibriSpeech/dev-clean'
    test = {'2428', '251', '3081', '3536', '6313', '652', '777', '8842'}
    spk = sorted(s for s in os.listdir(D) if s not in test)
    with Pool(4) as p: R = p.map(_tr_one, spk)
    bwe = BWE().fit(R); cb = fit_vq(R, bwe, 7)
    np.savez(out, W=np.array(bwe.W, dtype=object), cb=cb)

def _tr_one(s):
    D = '/tmp/claude-0/data/ls/LibriSpeech/dev-clean'
    rng = np.random.default_rng(int(s))
    files = list(rng.permutation(sorted(glob.glob(f'{D}/{s}/*/*.flac'))))
    parts = []; n = 0
    for f in files:
        x, _ = sf.read(f); parts.append(x); n += len(x)
        if n > 20 * 16000: break
    x = np.concatenate(parts)[:20 * 16000]; x = 0.5 * x / np.abs(resample_poly(x, 1, 2)).max()
    h = Hiss(); bits = h.encode(x, side=False); m = h._decode_model(bits)
    d = h._dstruct(bits, m, len(x))
    d['hb'] = bands_db(np.abs(stft(x)) ** 2, HB)[:len(d['E'])]
    return {k: d[k] for k in ('E', 'v', 'uu', 'frame', 'hb')}

if __name__ == '__main__':
    train()
