"""Oracle lab for the sotto encoder: take Codec 2's sinusoidal model (from c2sim --modelout),
change or quantize it in Python, and synthesise with c2sim --modelin --phase0 --postfilter.

The first candidate encoder ("sotto v0", 48 bits / 20 ms, memoryless):
  voicing  2 bits  (one per 10 ms subframe, as Codec 2 2400)
  Wo       7 bits  (log-uniform between Codec 2's Wo_min and Wo_max)
  energy   5 bits  (mean of the rate-K dB vector)
  shape   34 bits  (DCT of the mean-removed 20-point mel-spaced envelope, scalar
                    quantizers with trained step sizes and greedy bit allocation)
It replaces Codec 2 2400's LPC-10 / LSP envelope with a rate-K envelope, which the oracle
ladder showed is the biggest single loss.

Usage: python sim/vocoder_lab.py [--samples DIR]
"""
import argparse, os, subprocess, sys, tempfile
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audio_race as ar

MAX_AMP = 160
MODEL = np.dtype([("Wo", "<f4"), ("L", "<i4"), ("A", "<f4", MAX_AMP + 1),
                  ("phi", "<f4", MAX_AMP + 1), ("voiced", "<i4")])
FS = 8000
WO_MIN, WO_MAX = 2 * np.pi / 160, 2 * np.pi / 20
K = 20


def _mel(f):
    return np.floor(2595 * np.log10(1 + f / 700) + 0.5)


KFREQ = 700 * (10 ** (np.linspace(_mel(200), _mel(3700), K) / 2595) - 1)   # Hz
_n = np.arange(K)
DCT = np.sqrt(2 / K) * np.cos(np.pi * (_n[None, :] + 0.5) * _n[:, None] / K)
DCT[0] /= np.sqrt(2)                                     # orthonormal DCT-II


# ---------------------------------------------------------------- c2sim I/O
TRACK = {}
TRACK2 = {}


def analyse(x, pitch="c2"):
    """8 kHz float speech -> MODEL records, one per 10 ms. pitch="tracked" corrects
    Codec 2's octave errors with sinusoid.track_pitch and re-estimates the amplitudes."""
    if pitch == "tracked":
        import sinusoid as sn
        m = analyse(x)
        xi = np.round(x * 32767)
        Sw = sn.spectra(xi)
        wo = sn.track_pitch(Sw, m["Wo"], m["voiced"], **TRACK)
        return sn.analyse(xi, wo, voicing=m["voiced"], do_refine=False)[:len(m)]
    if pitch in ("tracked2", "tracked2t", "tracked3", "tracked4"):
        # v2: pitch up to 500 Hz, and voicing re-checked at the corrected pitch
        import sinusoid as sn
        m = analyse(x)
        xi = np.round(x * 32767)
        Sw = sn.spectra(xi)
        # v3.3: penalise sub-octave candidates (every other harmonic missing)
        opts = {"sub": 2.0, **TRACK2} if pitch in ("tracked3", "tracked4") else TRACK2
        if pitch == "tracked4":
            # v3.7: also a third of Codec 2's estimate as a candidate
            opts = {"third": True, **opts}
        wo, v = sn.track_pitch_voicing(Sw, m["Wo"], m["voiced"], **opts)
        m2 = sn.analyse(xi, wo, voicing=v, do_refine=False)[:len(m)]
        if pitch in ("tracked2t", "tracked3", "tracked4"):
            # v3.1: fading voiced-fricative tails played as noise
            m2["voiced"] = sn.unvoice_tails(Sw, m2)
        return m2
    with tempfile.TemporaryDirectory() as d:
        np.round(x * 32767).astype("<i2").tofile(f"{d}/in.raw")
        subprocess.run([f"{ar.C2BIN}/c2sim", f"{d}/in.raw", "--modelout", f"{d}/m.bin"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return np.fromfile(f"{d}/m.bin", MODEL)


def synthesise(m, postfilter=True):
    with tempfile.TemporaryDirectory() as d:
        n = len(m)
        np.zeros(n * 80, "<i2").tofile(f"{d}/in.raw")
        m.tofile(f"{d}/m.bin")
        args = [f"{ar.C2BIN}/c2sim", f"{d}/in.raw", "--modelin", f"{d}/m.bin", "--phase0",
                "-o", f"{d}/out.raw"] + (["--postfilter"] if postfilter else [])
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return np.fromfile(f"{d}/out.raw", "<i2").astype(float) / 32768


# ---------------------------------------------------------------- rate-K envelope
def to_rate_k(m):
    out = np.zeros((len(m), K))
    for i, r in enumerate(m):
        L = r["L"]
        f = np.arange(1, L + 1) * r["Wo"] * FS / (2 * np.pi)
        a = 20 * np.log10(r["A"][1:L + 1] + 1e-16)
        a = np.maximum(a, a.max() - 50)
        out[i] = np.interp(KFREQ, f, a)
    return out


def from_rate_k(m, rk):
    m = m.copy()
    for i in range(len(m)):
        L = m[i]["L"]
        f = np.arange(1, L + 1) * m[i]["Wo"] * FS / (2 * np.pi)
        A = np.zeros(MAX_AMP + 1, np.float32)
        A[1:L + 1] = 10 ** (np.interp(f, KFREQ, rk[i]) / 20)
        m[i]["A"] = A
    return m


def set_wo(m, wo, voiced):
    m = m.copy()
    m["Wo"] = wo
    m["L"] = np.floor(np.pi / wo).astype(int)
    m["voiced"] = voiced
    return m


def decimate_interp(v, axis_vals=None):
    """Keep every 2nd frame (20 ms) and linearly interpolate the frames in between."""
    out = v.copy()
    n = len(v)
    for i in range(1, n, 2):
        nxt = v[i + 1] if i + 1 < n else v[i - 1]
        out[i] = 0.5 * (v[i - 1] + nxt)
    return out


def interp_wo(wo, voiced, guard=False):
    """Codec 2 style: odd 10 ms subframes take Wo from the 20 ms frames either side.
    guard (v3.8): a voiced second half whose neighbours are both unvoiced keeps its own
    frame's Wo instead of the next frame's, whose pitch field is a hiss index."""
    out = wo.copy()
    for i in range(1, len(wo), 2):
        a, b = i - 1, min(i + 1, len(wo) - 1)
        if voiced[i] and voiced[a] and voiced[b]:
            out[i] = 0.5 * (wo[a] + wo[b])
        elif voiced[i]:
            out[i] = wo[a] if (voiced[a] or (guard and not voiced[b])) else wo[b]
        else:
            out[i] = WO_MIN * 2 if not voiced[i] else out[i]
    return out


# ---------------------------------------------------------------- quantizers
def q_uniform(x, lo, hi, bits):
    n = 2 ** bits
    idx = np.clip(np.round((x - lo) / (hi - lo) * (n - 1)), 0, n - 1).astype(int)
    return idx, lo + idx * (hi - lo) / (n - 1)


def q_wo(wo, bits=7, hi=WO_MAX):
    idx, lw = q_uniform(np.log(wo), np.log(WO_MIN), np.log(hi), bits)
    return idx, np.exp(lw)


class ShapeQ:
    """DCT coefficients 1..K-1 of the mean-removed envelope, uniform scalar quantizers."""

    def __init__(self, train, total_bits=34, max_bits=7):
        c = train @ DCT.T
        self.c = c[:, 1:]
        self.steps = {}
        self.bits = np.zeros(K - 1, int)
        # MSE for each coefficient at each bit count, with the best step found by search
        self.mse = np.zeros((K - 1, max_bits + 1))
        for j in range(K - 1):
            x = self.c[:, j]
            self.mse[j, 0] = np.mean(x ** 2)
            for b in range(1, max_bits + 1):
                best = None
                for s in np.std(x) * np.geomspace(0.02, 3, 60):
                    e = np.mean((x - self._q(x, s, b)) ** 2)
                    if best is None or e < best[0]:
                        best = (e, s)
                self.mse[j, b], self.steps[(j, b)] = best
        for _ in range(total_bits):            # greedy: give each bit where it cuts MSE most
            gain = [self.mse[j, self.bits[j]] - self.mse[j, self.bits[j] + 1]
                    if self.bits[j] < max_bits else -1 for j in range(K - 1)]
            self.bits[int(np.argmax(gain))] += 1

    @staticmethod
    def _q(x, s, b):
        n = 2 ** b
        idx = np.clip(np.floor(x / s + n / 2), 0, n - 1)
        return (idx - n / 2 + 0.5) * s

    def quantize(self, shape):
        c = shape @ DCT.T
        cq = np.zeros_like(c)
        for j in range(K - 1):
            b = self.bits[j]
            if b:
                cq[:, j + 1] = self._q(c[:, j + 1], self.steps[(j, b)], b)
        return cq @ DCT


def postfilter_rk(shape, gain=1.5):
    """newamp1-style envelope postfilter on mean-removed rate-K vectors (energy kept)."""
    pre = 20 * np.log10(KFREQ / 300.0)
    v = shape + pre
    e0 = np.log10(np.sum(10 ** (v / 10), 1, keepdims=True))
    v = v * gain
    e1 = np.log10(np.sum(10 ** (v / 10), 1, keepdims=True))
    return v - 10 * (e1 - e0) - pre


def sotto_v0(m, shapeq, e_lo=0.0, e_hi=80.0, pf=0.0, alpha=0.0):
    """Quantize one clip's model with the 48-bit v0 scheme, decode, return new MODEL.
    pf: envelope postfilter gain (0 = off). alpha: inter-frame prediction of the shape
    (0 = memoryless; the ShapeQ must then be trained on prediction residuals)."""
    rk = to_rate_k(m)
    mean = rk.mean(1, keepdims=True)
    shape = rk - mean
    voiced = m["voiced"].copy()
    _, wo = q_wo(np.where(voiced > 0, m["Wo"], m["Wo"]), 7)
    _, meanq = q_uniform(mean[:, 0], e_lo, e_hi, 5)
    if alpha:
        shapeq_ = np.zeros_like(shape)
        prev = np.zeros(K)
        for i in range(0, len(shape), 2):
            shapeq_[i] = shapeq.quantize((shape[i] - alpha * prev)[None])[0] + alpha * prev
            prev = shapeq_[i]
    else:
        shapeq_ = shapeq.quantize(shape)
    if pf:
        shapeq_ = postfilter_rk(shapeq_, pf)
    rkq = decimate_interp(shapeq_ + meanq[:, None])
    wo = interp_wo(np.asarray(wo, float), voiced)
    return from_rate_k(set_wo(m, wo, voiced), rkq)


def training_set(pitch="c2"):
    """Rate-K shapes from Codec 2's test recordings, excluding the 15 s ve9qrp test window."""
    raw = os.path.join(os.path.dirname(HERE), "codec", "codec2", "raw")
    xs = []
    for f in ("hts1.raw", "kristoff.raw", "ve9qrp.raw"):
        x = np.fromfile(os.path.join(raw, f), "<i2").astype(float) / 32768
        if f == "ve9qrp.raw":
            x = np.concatenate([x[:int(6.4 * FS)], x[int(21.4 * FS):]])
        xs.append(x)
    rk = np.concatenate([to_rate_k(analyse(x, pitch)) for x in xs])
    rk = rk[rk.mean(1) > np.percentile(rk.mean(1), 20)]          # drop silence
    return rk - rk.mean(1, keepdims=True)


def predictive_training(shapes, alpha):
    """Residuals of first-order prediction at 20 ms spacing (open loop, for step training)."""
    s20 = shapes[::2]
    return s20[1:] - alpha * s20[:-1]


def main(samples):
    clips = ar.load_clips(samples)
    tr = training_set()
    sq = ShapeQ(tr)
    sqp = ShapeQ(predictive_training(tr, 0.5))
    print("shape bits per DCT coefficient:", sq.bits.tolist(), "=", sq.bits.sum())
    rows = {}
    for c, x in clips.items():
        m = analyse(x)
        rk = to_rate_k(m)
        variants = {
            "model, phase0+pf": m,
            "rate-K envelope (unquantized)": from_rate_k(m, rk),
            "rate-K, 20 ms + interp": from_rate_k(m, decimate_interp(rk)),
            "sotto v0 (48 bits)": sotto_v0(m, sq),
            "v0 + envelope postfilter 1.5": sotto_v0(m, sq, pf=1.5),
            "v0 + prediction 0.5": sotto_v0(m, sqp, alpha=0.5),
            "v0 + prediction 0.5 + pf 1.5": sotto_v0(m, sqp, alpha=0.5, pf=1.5),
        }
        rows[c] = {}
        for name, mm in variants.items():
            y = synthesise(mm)
            rows[c][name] = ar.score(x, y, ar.best_lag(x, y))
        y = ar.c2dec(ar.c2enc(np.round(x * 32767)))
        rows[c]["Codec 2 2400 (c2enc/c2dec)"] = ar.score(x, y, ar.best_lag(x, y))
    names = list(next(iter(rows.values())))
    print(f"{'variant':32s}" + "".join(f"{c[:18]:>20s}" for c in clips))
    for n in names:
        print(f"{n:32s}" + "".join(f"{rows[c][n][0]:12.2f} ({rows[c][n][1]:.2f})" for c in clips))
    return rows


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--samples", default=ar.SAMPLES)
    main(a.parse_args().samples)


# ---------------------------------------------------------------- v0 as a bitstream codec
def frame_energy_db(m):
    return np.array([10 * np.log10(np.sum(r["A"][1:r["L"] + 1] ** 2) + 1e-9) for r in m])


def set_energy(m, e_db):
    m = m.copy()
    for i in range(len(m)):
        L = m[i]["L"]
        e = np.sum(m[i]["A"][1:L + 1] ** 2) + 1e-12
        m[i]["A"][1:L + 1] *= np.sqrt(10 ** (e_db[i] / 10) / e)
    return m


class SottoV0:
    """Memoryless 48-bit / 20 ms codec: bits = voicing(2) | Wo(7) | energy(5) | shape(34).
    Most sensitive fields first. Encoder runs c2sim analysis; decoder needs only the bits.
    The energy field is the frame energy (sum of harmonic powers), so the decoded level
    matches the input whatever the envelope smoothing does."""

    E_LO, E_HI = 10.0, 80.0
    WO_HI = WO_MAX
    SILENT0 = False             # energy code 0 decodes as silence (v3 on)
    ENERGY_RK = False           # energy field from the rate-K envelope only (v3.6 on)

    def __init__(self, shapeq=None, pf=1.5, pitch="c2"):
        self.pitch = pitch
        self.sq = shapeq or ShapeQ(training_set(pitch))
        self.pf = pf
        self.fields = [b for b in self.sq.bits if b]

    def _shape_idx(self, shape):
        c = shape @ DCT.T
        idx = []
        for j in range(K - 1):
            b = self.sq.bits[j]
            if b:
                n = 2 ** b
                idx.append(int(np.clip(np.floor(c[j + 1] / self.sq.steps[(j, b)] + n / 2), 0, n - 1)))
        return idx

    def _shape_val(self, idx):
        c = np.zeros(K)
        it = iter(idx)
        for j in range(K - 1):
            b = self.sq.bits[j]
            if b:
                c[j + 1] = (next(it) - 2 ** b / 2 + 0.5) * self.sq.steps[(j, b)]
        return c @ DCT

    @staticmethod
    def _pack(vals_bits):
        out = []
        for v, b in vals_bits:
            out += [(int(v) >> (b - 1 - i)) & 1 for i in range(b)]
        return out

    def encode(self, x):
        m = analyse(x, self.pitch)
        if len(m) % 2:
            m = np.concatenate([m, m[-1:]])
        rk = to_rate_k(m)
        # v3.6: code the energy the decoder can rebuild (the envelope starts at 200 Hz), so a
        # strong component below it isn't spread over the whole band
        en = frame_energy_db(from_rate_k(m, rk) if self.ENERGY_RK else m)
        frames = []
        for i in range(0, len(m), 2):
            v = [int(m[i]["voiced"] > 0), int(m[i + 1]["voiced"] > 0)]
            wi, _ = q_wo(np.array([m[i]["Wo"]]), hi=self.WO_HI)
            ei, _ = q_uniform(np.array([en[i]]), self.E_LO, self.E_HI, 5)
            si = self._shape_idx(rk[i] - rk[i].mean())
            frames.append(self._pack([(v[0], 1), (v[1], 1), (wi[0], 7), (ei[0], 5)]
                                     + list(zip(si, self.fields))))
        return np.array(frames, np.int8)

    def decode(self, bits):
        n = len(bits)
        voiced = np.zeros(2 * n, int)
        wo = np.zeros(2 * n)
        rk = np.zeros((2 * n, K))
        en = np.zeros(2 * n)
        for f, b in enumerate(np.asarray(bits)):
            p = 0

            def take(k):
                nonlocal p
                v = int("".join(map(str, b[p:p + k])), 2)
                p += k
                return v
            voiced[2 * f], voiced[2 * f + 1] = self._read_voicing(take(2), voiced[2 * f - 1] if f else 0)
            wo[2 * f] = np.exp(np.log(WO_MIN) + take(7) * (np.log(self.WO_HI) - np.log(WO_MIN)) / 127)
            ei = take(5)
            en[2 * f] = -60.0 if ei == 0 and self.SILENT0 else self.E_LO + ei * (self.E_HI - self.E_LO) / 31
            shape = self._shape_val([take(bb) for bb in self.fields])
            if self.pf:
                shape = postfilter_rk(shape[None], self.pf)[0]
            rk[2 * f] = shape
        rk = decimate_interp(rk)
        en = decimate_interp(en)
        wo[1::2] = wo[0::2]
        wo = interp_wo(wo, voiced, getattr(self, "UU_WO_GUARD", False))
        wo = np.where(voiced > 0, wo, np.maximum(wo, WO_MIN))
        m = np.zeros(2 * n, MODEL)
        m = set_wo(m, wo, np.minimum(voiced, 1))
        m = set_energy(from_rate_k(m, rk), en)
        for k in np.where(voiced == MIXED)[0]:
            m[k]["voiced"] = mixed_cutoff(m[k]["Wo"])
        return m

    @staticmethod
    def _read_voicing(code, prev):
        """2-bit voicing field -> flags for the two 10 ms halves (v0-v3.1: one bit each)."""
        return (code >> 1) & 1, code & 1

    def decode_audio(self, bits):
        return synthesise(self.decode(bits))


# ---------------------------------------------------------------- v1: tracked pitch, predicted shape
def training_pairs(pitch="tracked"):
    """DCT coefficient vectors of consecutive 20 ms frames (both non-silent) from the
    training recordings, for fitting the shape predictor."""
    raw = os.path.join(os.path.dirname(HERE), "codec", "codec2", "raw")
    prev, cur = [], []
    for f in ("hts1.raw", "kristoff.raw", "ve9qrp.raw"):
        x = np.fromfile(os.path.join(raw, f), "<i2").astype(float) / 32768
        segs = [x] if f != "ve9qrp.raw" else [x[:int(6.4 * FS)], x[int(21.4 * FS):]]
        for s in segs:
            rk = to_rate_k(analyse(s, pitch))[::2]
            mean = rk.mean(1)
            c = (rk - mean[:, None]) @ DCT.T
            live = mean > np.percentile(mean, 20)
            ok = live[1:] & live[:-1]
            prev.append(c[:-1][ok])
            cur.append(c[1:][ok])
    return np.concatenate(prev), np.concatenate(cur)


class SottoV1(SottoV0):
    """v0 with the octave-corrected pitch track and a first-order predictor on the shape's
    DCT coefficients (per-coefficient gains alpha_j, fitted on the training set and scaled
    by `shrink` so channel errors die out quickly). The shape quantizer is trained on the
    prediction residuals. Bit layout is unchanged from v0.

    decode(bits, good) leaves the predictor state untouched on frames flagged bad, so a
    concealed frame doesn't feed its residual in again."""

    def __init__(self, shrink=0.8, pf=1.5, pitch="tracked", max_alpha=0.8):
        p, c = training_pairs(pitch)
        a = np.sum(p * c, 0) / np.maximum(np.sum(p * p, 0), 1e-9)
        self.alpha = np.clip(a * shrink, 0, max_alpha)
        self.alpha[0] = 0
        res = (c - self.alpha * p) @ DCT
        super().__init__(ShapeQ(res), pf=pf, pitch=pitch)

    def _shape_idx_c(self, c):
        return self._shape_idx(c @ DCT)

    def encode(self, x):
        m = analyse(x, self.pitch)
        if len(m) % 2:
            m = np.concatenate([m, m[-1:]])
        rk = to_rate_k(m)
        en = frame_energy_db(m)
        prev = np.zeros(K)
        frames = []
        for i in range(0, len(m), 2):
            v = [int(m[i]["voiced"] > 0), int(m[i + 1]["voiced"] > 0)]
            wi, _ = q_wo(np.array([m[i]["Wo"]]))
            ei, _ = q_uniform(np.array([en[i]]), self.E_LO, self.E_HI, 5)
            c = (rk[i] - rk[i].mean()) @ DCT.T
            pred = self.alpha * prev
            si = self._shape_idx_c(c - pred)
            prev = pred + self._shape_val(si) @ DCT.T
            frames.append(self._pack([(v[0], 1), (v[1], 1), (wi[0], 7), (ei[0], 5)]
                                     + list(zip(si, self.fields))))
        return np.array(frames, np.int8)

    def decode(self, bits, good=None):
        bits = np.asarray(bits)
        good = np.ones(len(bits), bool) if good is None else np.asarray(good, bool)
        nshape = sum(self.fields)
        prev = np.zeros(K)
        out = bits.copy()
        shapes = []
        for f, b in enumerate(bits):
            if good[f]:
                p = 14
                idx = []
                for bb in self.fields:
                    idx.append(int("".join(map(str, b[p:p + bb])), 2))
                    p += bb
                prev = self.alpha * prev + self._shape_val(idx) @ DCT.T
            shapes.append(prev.copy())
        # reuse v0's decoder with a memoryless shape quantizer that returns these shapes
        m = SottoV0.decode(self, out)
        rk = np.zeros((2 * len(bits), K))
        rk[0::2] = np.array(shapes) @ DCT
        if self.pf:
            rk[0::2] = postfilter_rk(rk[0::2], self.pf)
        rk = decimate_interp(rk)
        en = frame_energy_db(m)
        return set_energy(from_rate_k(m, rk), en)


class SottoV2(SottoV0):
    """v1 plus pitch up to 500 Hz (the 7-bit Wo field now spans 50-500 Hz) and voicing
    re-checked at the corrected pitch, with voiced runs under 30 ms dropped."""

    WO_HI = 2 * np.pi / 16
    PITCH = "tracked2"

    def __init__(self, shapeq=None, pf=1.5, quiet=None):
        super().__init__(shapeq, pf=pf, pitch=self.PITCH)
        self.quiet = quiet          # dict(margin, tilt, atten): see soften_pauses

    def decode(self, bits):
        m = super().decode(bits)
        return soften_pauses(m, **self.quiet) if self.quiet else m


def soften_pauses(m, margin=25.0, tilt=6.0, atten=0.0, release=0.995):
    """Decoder-only treatment of background between words: unvoiced frames more than
    `margin` dB below the recent speech peak get a high-frequency roll-off of `tilt` dB per
    octave above 1 kHz and `atten` dB less level. Codec 2's softer, darker pauses were
    preferred by a listener over v2's flat hiss."""
    m = m.copy()
    en = frame_energy_db(m)
    peak = -1e9
    for i in range(len(m)):
        peak = max(en[i], peak * release + (1 - release) * en[i] if peak > -1e8 else en[i])
        if m[i]["voiced"] or en[i] > peak - margin:
            continue
        L = m[i]["L"]
        f = np.arange(1, L + 1) * m[i]["Wo"] * FS / (2 * np.pi)
        g = -tilt * np.log2(np.maximum(f, 1000) / 1000)
        a = m[i]["A"][1:L + 1] * 10 ** (g / 20)
        a *= np.sqrt(np.sum(m[i]["A"][1:L + 1] ** 2) / (np.sum(a ** 2) + 1e-12)) * 10 ** (-atten / 20)
        m[i]["A"][1:L + 1] = a
    return m


class SottoV2P(SottoV2):
    """v2 with the shape predicted from the previous frame, except on reset frames: frames
    whose two voicing bits are both unvoiced, and every `every`-th frame (None = never).
    Reset frames use v2's memoryless quantizer, the rest a quantizer trained on prediction
    residuals, so the bit layout doesn't change. Errors from a lost frame stop at the next
    reset. decode(bits, good) holds the predictor on frames flagged bad."""

    def __init__(self, every=10, shrink=1.0, max_alpha=0.8, pf=1.5, quiet=None):
        super().__init__(pf=pf, quiet=quiet)
        self.every = every
        p, c = training_pairs("tracked2")
        a = np.sum(p * c, 0) / np.maximum(np.sum(p * p, 0), 1e-9)
        self.alpha = np.clip(a * shrink, 0, max_alpha)
        self.alpha[0] = 0
        self.sq_abs, self.sq_res = self.sq, ShapeQ((c - self.alpha * p) @ DCT)

    def _reset(self, f, v0, v1):
        return bool((not v0 and not v1) or (self.every and f % self.every == 0))

    def _use(self, sq):
        self.sq, self.fields = sq, [b for b in sq.bits if b]

    def encode(self, x):
        m = analyse(x, self.pitch)
        if len(m) % 2:
            m = np.concatenate([m, m[-1:]])
        rk = to_rate_k(m)
        en = frame_energy_db(m)
        prev = np.zeros(K)
        frames = []
        for f, i in enumerate(range(0, len(m), 2)):
            v = [int(m[i]["voiced"] > 0), int(m[i + 1]["voiced"] > 0)]
            wi, _ = q_wo(np.array([m[i]["Wo"]]), hi=self.WO_HI)
            ei, _ = q_uniform(np.array([en[i]]), self.E_LO, self.E_HI, 5)
            c = (rk[i] - rk[i].mean()) @ DCT.T
            if self._reset(f, *v):
                self._use(self.sq_abs)
                pred = np.zeros(K)
            else:
                self._use(self.sq_res)
                pred = self.alpha * prev
            si = self._shape_idx((c - pred) @ DCT)
            prev = pred + self._shape_val(si) @ DCT.T
            frames.append(self._pack([(v[0], 1), (v[1], 1), (wi[0], 7), (ei[0], 5)]
                                     + list(zip(si, self.fields))))
        self._use(self.sq_abs)
        return np.array(frames, np.int8)

    def decode(self, bits, good=None):
        bits = np.asarray(bits)
        good = np.ones(len(bits), bool) if good is None else np.asarray(good, bool)
        prev = np.zeros(K)
        shapes = []
        for f, b in enumerate(bits):
            if good[f]:
                reset = self._reset(f, b[0], b[1])
                self._use(self.sq_abs if reset else self.sq_res)
                p, idx = 14, []
                for bb in self.fields:
                    idx.append(int("".join(map(str, b[p:p + bb])), 2))
                    p += bb
                prev = (0 if reset else self.alpha * prev) + self._shape_val(idx) @ DCT.T
            shapes.append(prev.copy())
        self._use(self.sq_abs)
        m = SottoV0.decode(self, bits)                 # voicing, pitch, energy
        rk = np.zeros((2 * len(bits), K))
        rk[0::2] = np.array(shapes) @ DCT
        if self.pf:
            rk[0::2] = postfilter_rk(rk[0::2], self.pf)
        m = set_energy(from_rate_k(m, decimate_interp(rk)), frame_energy_db(m))
        return soften_pauses(m, **self.quiet) if self.quiet else m


# ---------------------------------------------------------------- multistage VQ for the shape
class ShapeMSVQ:
    """Multistage vector quantizer for the mean-removed rate-K shape (K = 20, DC removed, so
    it works on DCT coefficients 1..K-1). Stage s has 2**bits[s] codewords; each stage codes
    the residual of the ones before. Training is k-means per stage, then a few passes of
    joint re-estimation. The search keeps the M best paths through the stages (M-best), as
    in MELPe's LSF quantizer. Distortion is squared error in dB, so every rate-K point
    weighs the same."""

    def __init__(self, train, bits=(12, 11, 11), m_best=8, iters=25, joint=3, seed=0):
        from sklearn.cluster import KMeans
        self.bits, self.m = tuple(bits), m_best
        c = (train @ DCT.T)[:, 1:]
        r = c.copy()
        self.cb = []
        for b in bits:
            km = KMeans(2 ** b, n_init=1, max_iter=iters, random_state=seed).fit(r)
            self.cb.append(km.cluster_centers_)
            r = r - self.cb[-1][km.labels_]
        for _ in range(joint):                   # re-estimate each stage given the others
            idx = self._search(c)
            for s in range(len(bits)):
                others = c - sum(self.cb[t][idx[:, t]] for t in range(len(bits)) if t != s)
                for k in range(2 ** bits[s]):
                    sel = idx[:, s] == k
                    if sel.any():
                        self.cb[s][k] = others[sel].mean(0)

    def _search(self, c, chunk=2000):
        c = np.atleast_2d(c)
        if len(c) > chunk:
            return np.concatenate([self._search(c[i:i + chunk]) for i in range(0, len(c), chunk)])
        n = len(c)
        paths = np.zeros((n, 1, 0), int)
        recon = np.zeros((n, 1, c.shape[1]))
        for cb in self.cb:
            res = c[:, None, :] - recon                               # n, P, D
            d = (res ** 2).sum(-1)[..., None] - 2 * res @ cb.T + (cb ** 2).sum(1)  # n, P, C
            C = d.shape[2]
            keep = np.argpartition(d.reshape(n, -1), min(self.m, d[0].size) - 1, 1)[:, :self.m]
            pi, ci = keep // C, keep % C
            paths = np.concatenate([np.take_along_axis(paths, pi[..., None], 1), ci[..., None]], 2)
            recon = np.take_along_axis(recon, pi[..., None], 1) + cb[ci]
        best = ((c[:, None, :] - recon) ** 2).sum(-1).argmin(1)
        return paths[np.arange(n), best]

    @classmethod
    def load(cls, path=os.path.join(HERE, "data", "shape_msvq_34.npz")):
        d = np.load(path)
        q = cls.__new__(cls)
        q.bits, q.m = tuple(int(b) for b in d["bits"]), int(d["m_best"])
        q.cb = [d[f"stage{i}"].astype(float) for i in range(len(q.bits))]
        return q

    def save(self, path):
        np.savez_compressed(path, bits=np.array(self.bits), m_best=self.m,
                            **{f"stage{i}": cb.astype(np.float32) for i, cb in enumerate(self.cb)})

    def indices(self, shape):
        return self._search((np.atleast_2d(shape) @ DCT.T)[:, 1:])

    def value(self, idx):
        idx = np.atleast_2d(idx)
        c = np.zeros((len(idx), K))
        c[:, 1:] = sum(self.cb[s][idx[:, s]] for s in range(len(self.cb)))
        return c @ DCT

    def quantize(self, shape):
        return self.value(self.indices(shape))


class SottoV3(SottoV2):
    """v2 with the 34 shape bits sent as multistage VQ indices instead of scalar DCT
    coefficients, and energy code 0 decoding as silence. Same 48-bit frame: voicing 2,
    pitch 7, energy 5, shape 12 + 11 + 11. The default codebook (sim/data/shape_msvq_34.npz)
    is trained by tools/train_shape_vq.py on 600 Speech Commands speakers."""

    SILENT0 = True

    def __init__(self, vq=None, pf=1.5, quiet=None):
        vq = vq or ShapeMSVQ.load()
        self.vq = vq
        super().__init__(shapeq=_VQAdapter(vq), pf=pf, quiet=quiet)


class SottoV31(SottoV3):
    """v3 with sinusoid.unvoice_tails in the encoder: the fading ends of voiced fricatives
    are sent as unvoiced. Same frame and decoder as v3. Listening round 5: preferred on the
    "five" of one voice, no difference on four others, never worse."""

    PITCH = "tracked2t"


MIXED = 2                       # decoder-internal voicing state: harmonics below MIX_HZ only
MIX_HZ = 1000.0


def mixed_cutoff(wo):
    """MODEL 'voiced' value for mixed excitation: phase.c treats voiced >= 2 as the first
    harmonic index synthesised as noise."""
    return max(2, int(MIX_HZ / (wo * FS / (2 * np.pi))) + 1)


def voicing_classes(x, m):
    """Per 10 ms frame: 0 unvoiced, 1 voiced, MIXED where the frame is voiced but the
    harmonic fit below 1 kHz is weak (mean per-harmonic SNR < 8 dB): voiced fricatives,
    breathy and fading vowels."""
    import sinusoid as sn
    Sw = sn.spectra(np.round(x * 32767))
    c = (m["voiced"] > 0).astype(int)
    for k in np.where(c)[0]:
        wo = m["Wo"][k]
        L = sn.n_harm(wo)
        f = np.arange(L + 1) * wo * FS / (2 * np.pi)
        if sn.harmonic_snr(Sw[k], wo, L)[(f > 0) & (f < 1000)].mean() < 8.0:
            c[k] = MIXED
    return c


class SottoV32(SottoV31):
    """v3.1 with mixed excitation and no extra bits. The 2-bit voicing field becomes one
    code per 20 ms frame instead of one bit per 10 ms half:
        0 = U U    1 = V V    2 = M M    3 = an edge: one half unvoiced, the other mixed,
    where M means harmonics below 1 kHz and noise above. The order of an edge follows the
    previous frame: after an unvoiced half it is an onset (U M), otherwise an offset (M U).
    Fully voiced/unvoiced edges (U V, V U) are only ~1% of frames; they become U M / M U.
    V M and M V (~9%) are sent as V V."""

    def encode(self, x):
        bits = super().encode(x)
        m = analyse(x, self.PITCH)
        c = voicing_classes(x, m)
        if len(c) % 2:
            c = np.concatenate([c, c[-1:]])
        prev = 0
        for f in range(len(bits)):
            a, b = c[2 * f], c[2 * f + 1]
            if a == b:
                code = a
            elif 0 in (a, b):
                code = 3
            else:
                code = 1
            bits[f, 0], bits[f, 1] = code >> 1, code & 1
            prev = self._read_voicing(code, prev)[1]
        return bits

    @staticmethod
    def _read_voicing(code, prev):
        if code < 3:
            return code, code
        return (0, MIXED) if prev == 0 else (MIXED, 0)


class SottoV33(SottoV32):
    """v3.2 with the pitch tracker's sub-octave check (sinusoid.odd_even_db): a candidate
    whose odd harmonics sit far below its even ones is half the true pitch. Fixes a
    "grumbly" word where v2-v3.2 tracked 129 Hz for a 258 Hz voice."""

    PITCH = "tracked3"


class SottoV34(SottoV33):
    """v3.3 with concealment in the decoder. A lost frame (CRC failure) replays the last
    good frame's parameters, not its bits' audio: the level falls `slope` dB per 20 ms
    after the first lost frame, voiced frames turn mixed from the `mix_after`-th lost frame
    (a held buzz is what makes "springs"), and after `max_lost` frames the output is
    silent until a good frame arrives. The first `hold` lost frames keep full level.
    Needs the receiver's good flags."""

    def __init__(self, vq=None, pf=1.5, quiet=None, slope=2.0, mix_after=2, max_lost=15, hold=1):
        super().__init__(vq=vq, pf=pf, quiet=quiet)
        self.slope, self.mix_after, self.max_lost, self.hold = slope, mix_after, max_lost, hold

    def decode(self, bits, good=None):
        bits = np.asarray(bits)
        if good is None or np.all(good):
            return super().decode(bits)
        bits = bits.copy()
        run = np.zeros(len(bits), int)
        last, r = None, 0
        for f in range(len(bits)):
            if good[f]:
                last, r = bits[f].copy(), 0
            else:
                r += 1
                run[f] = r
                if last is not None:
                    bits[f] = last
        m = super().decode(bits)
        for f in np.where(run > 0)[0]:
            for k in (2 * f, 2 * f + 1):
                if k >= len(m):
                    continue
                j = run[f]
                if j > self.max_lost:
                    g = 1e-4
                else:
                    g = 10 ** (-self.slope * max(0, j - self.hold + (k - 2 * f) / 2) / 20)
                    if j >= self.mix_after and m[k]["voiced"] == 1:
                        m[k]["voiced"] = mixed_cutoff(m[k]["Wo"])
                m[k]["A"] *= g
        return m


class SottoV35(SottoV34):
    """v3.4 with a lighter formant postfilter (gain 1.35 instead of 1.5) and short
    concealment. The postfilter was re-tuned for the trained codebook: +0.02 PESQ and +0.01
    ESTOI over 31 voices, and the shallower valleys keep more of the 1-2 kHz frication in
    sounds like the "v" of "five". Concealment holds a lost frame for 40 ms, fades 6 dB per
    20 ms, and is silent after 80 ms: listening round 8 preferred it to v3.3's
    repeat-then-mute or tied, and round 7 found v3.4's 300 ms tail made drops obvious."""

    def __init__(self, vq=None, pf=1.35, quiet=None, hold=2, slope=6.0, mix_after=99, max_lost=4):
        super().__init__(vq=vq, pf=pf, quiet=quiet, hold=hold, slope=slope,
                         mix_after=mix_after, max_lost=max_lost)


def round_codebook(vq, bits=8):
    """Copy of a ShapeMSVQ with each stage's values rounded to signed `bits`-bit integers
    times one scale per stage, as a fixed-point DSP would store them."""
    q = ShapeMSVQ.__new__(ShapeMSVQ)
    q.bits, q.m = vq.bits, vq.m
    lim = 2 ** (bits - 1) - 1
    q.cb = [(np.round(c / (np.abs(c).max() / lim)) * (np.abs(c).max() / lim)).astype(np.float32).astype(float)
            for c in vq.cb]
    return q


class SottoV36(SottoV35):
    """v3.5 for the C5517: the 9+9+8+8 shape codebook (sim/data/shape_msvq_9988.npz) in
    8-bit values, 29 KB instead of 152 KB (listening round 9: 5 ties of 5), and the energy
    field measured on the rate-K envelope (200-3700 Hz) rather than on all harmonics. The
    second change fixes a buzz where a strong hum below 200 Hz had its energy spread over
    the whole band; over 19 voices PESQ 2.917 -> 2.939, ESTOI 0.800 -> 0.818. Same frame."""

    ENERGY_RK = True

    def __init__(self, vq=None, **kw):
        vq = vq or round_codebook(ShapeMSVQ.load(os.path.join(HERE, "data", "shape_msvq_9988.npz")))
        super().__init__(vq=vq, **kw)


class SottoV37(SottoV36):
    """v3.6 with one more pitch candidate in the encoder: a third of Codec 2's estimate
    (sinusoid.track_pitch_voicing third=True). Codec 2 sometimes says three times the true
    pitch for several frames, and the tracker then had no candidate near the true pitch:
    listening round 10, pair 2 (EARS p020) jumped to ~490 Hz for 100 ms at 0.4 s, heard as
    high-pitched pops. Gross pitch errors over 19 voices 51 -> 26 frames. Same frame and
    decoder as v3.6."""

    PITCH = "tracked4"


class _VQAdapter:
    """Lets SottoV0's packing code treat the VQ stages as its 'fields'."""

    def __init__(self, vq):
        self.vq = vq
        self.bits = np.array(vq.bits)


def _v3_shape_idx(self, shape):
    if isinstance(self.sq, _VQAdapter):
        return [int(i) for i in self.sq.vq.indices(shape)[0]]
    return SottoV0._shape_idx_scalar(self, shape)


def _v3_shape_val(self, idx):
    if isinstance(self.sq, _VQAdapter):
        return self.sq.vq.value(np.array(idx))[0]
    return SottoV0._shape_val_scalar(self, idx)


SottoV0._shape_idx_scalar, SottoV0._shape_val_scalar = SottoV0._shape_idx, SottoV0._shape_val
SottoV0._shape_idx, SottoV0._shape_val = _v3_shape_idx, _v3_shape_val
