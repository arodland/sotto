from common import *
DCT = vl.DCT

class Proto(vl.SottoV37):
    def __init__(self, uv_wo=None, uv_gain=0.0, cb5=None, oracle=False, **kw):
        super().__init__(**kw)
        self.uv_wo, self.uv_gain, self.cb5, self.oracle = uv_wo, uv_gain, cb5, oracle
        self._or = {}

    def uu_targets(self, x):
        m = vl.analyse(x, self.PITCH)
        if len(m) % 2: m = np.concatenate([m, m[-1:]])
        rk = vl.to_rate_k(m)
        return rk - rk.mean(1, keepdims=True)

    def encode(self, x):
        bits = super().encode(x)
        if self.cb5 is None and not self.oracle:
            return bits
        sh = self.uu_targets(x)
        for f in range(len(bits)):
            if bits[f, 0] or bits[f, 1]:
                continue
            t = sh[2 * f]
            if self.oracle:
                self._or[f] = t; continue
            idx = [int(''.join(map(str, bits[f, 14 + sum(self.fields[:j]):14 + sum(self.fields[:j + 1])])), 2) for j in range(len(self.fields))]
            r = ((t - self._shape_val(idx)) @ DCT.T)[1:]
            i5 = int(np.argmin(((self.cb5 - r) ** 2).sum(1)))
            bits[f, 2:9] = [(i5 >> (6 - b)) & 1 for b in range(7)]
        return bits

    def _frame_hook(self, f, vcode, wi, shape):
        if vcode == 0:
            if self.oracle and f in self._or: return self._or[f]
            if self.cb5 is not None:
                c = np.zeros(vl.K); c[1:] = self.cb5[wi]; return shape + c @ DCT
        return shape

    def _model_hook(self, m, rk, en):
        u = np.where(m['voiced'] == 0)[0]
        if self.uv_wo is not None and len(u):
            mu = vl.set_wo(m[u], np.full(len(u), self.uv_wo), np.zeros(len(u), int))
            m[u] = vl.set_energy(vl.from_rate_k(mu, rk[u]), en[u])
        if self.uv_gain:
            for k in u: m[k]['A'] *= 10 ** (self.uv_gain / 20)
        return m

KLO = vl.KFREQ < 1000; KHI = vl.KFREQ > 2000

class Proto2(Proto):
    """Proto + fixes for vowel onsets coded unvoiced (Jeff's "act" scratch, round 12):
    tilt_gate: the +uv_gain only on unvoiced frames whose envelope is not low-heavy;
    onset_m: encoder recodes UU frames with a loud, low-heavy envelope as MM, with the pitch
    of the nearest voiced frame (within 3 frames)."""
    def __init__(self, tilt_gate=False, onset_m=False, lowdom=3.0, loud_db=10.0, **kw):
        super().__init__(**kw); self.tilt_gate, self.onset_m, self.lowdom, self.loud_db = tilt_gate, onset_m, lowdom, loud_db
        self.recoded = []

    def encode(self, x):
        bits = super().encode(x)
        if not self.onset_m: return bits
        m = vl.analyse(x, self.PITCH)
        if len(m) % 2: m = np.concatenate([m, m[-1:]])
        rk = vl.to_rate_k(m); en = rk.mean(1)
        loud = en > np.percentile(en, 90) - self.loud_db
        code = bits[:, 0] * 2 + bits[:, 1]; self.recoded = []
        for f in range(len(bits)):
            if code[f] != 0: continue
            k = 2 * f
            if not (loud[k] and rk[k, KLO].mean() - rk[k, KHI].mean() > self.lowdom): continue
            near = [g for d in (1, -1, 2, -2, 3, -3) for g in [f + d] if 0 <= g < len(bits) and code[g] in (1, 2)]
            if not near: continue
            bits[f, 0], bits[f, 1] = 1, 0                # MM
            bits[f, 2:9] = bits[near[0], 2:9]
            self.recoded.append(f)
        return bits

    def _model_hook(self, m, rk, en):
        if not self.tilt_gate:
            return super()._model_hook(m, rk, en)
        g, self.uv_gain = self.uv_gain, 0.0
        m = super()._model_hook(m, rk, en); self.uv_gain = g
        for k in np.where(m['voiced'] == 0)[0]:
            if rk[k, KLO].mean() - rk[k, KHI].mean() < 0:      # high-tilted: a real fricative
                m[k]['A'] *= 10 ** (g / 20)
        return m
