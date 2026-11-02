"""sotto v3.8: unvoiced consonants (docs/unvoiced-hiss.md).

Same 48-bit frame as v3.7, 16 kHz in and out:
  - unvoiced halves are synthesised as noise harmonics 50 Hz apart (not at the coded pitch),
    so the 7 pitch bits of an all-unvoiced (UU) frame carry nothing. They now carry a 7-bit
    index for the 4-8 kHz band: four band levels (4-5, 5-6, 6-7, 7-8 kHz) as a correction
    to a guess the decoder makes from the 0-4 kHz envelope;
  - unvoiced frames whose envelope tilts upward (real fricatives) get +3 dB, which undoes
    the level the noise synthesis loses (listening round 11);
  - encoder: a frame where 4-8 kHz jumps by 12 dB (a t/p/k burst) is sent as UU so it
    gets an index; a loud, low-heavy frame the tracker called UU (a creaky vowel onset,
    "act" in round 12) is sent as MM with its neighbour's pitch;
  - decoder: 4-8 kHz noise in UU frames (and in the unvoiced half of a neighbouring edge
    frame) at the coded level, and in mixed frames at the guess - 3 dB. Lost frames use
    the concealed bits and the concealment fade.
The 0-4 kHz part is v3.7 upsampled; a v3.7 decoder reads a v3.8 stream with rougher noise
in UU frames and no high band.
"""
import os
import numpy as np
from scipy.signal import resample_poly

import vocoder_lab as vl

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "data", "hiss_band.npz")

N, H = 320, 160                              # 20 ms window, 10 ms hop at 16 kHz
WIN = np.sqrt(np.hanning(N + 1)[:N])         # sqrt-Hann: squares sum to 1 at 50% overlap
FR = np.fft.rfftfreq(N, 1 / 16000)
HB = [(4000, 5000), (5000, 6000), (6000, 7000), (7000, 8000)]
HBC = np.array([4500.0, 5500, 6500, 7500])
FB = np.array([1250.0, 1750, 2250, 2750, 3250, 3600, 3800])   # guess inputs (Hz)
HB_LAG = 2                                   # STFT frame t of the output = model frame t + 2
W50 = 2 * np.pi / 160
UV_GAIN = 3.0
BURST_DB = 12.0
MIXED_DB = -3.0
# vowel template (voiced frames): 4-8 kHz band levels relative to the decoded 3.0-3.5 kHz
# level, a_b + c_b * (slope of 2-3.5 kHz in dB/kHz); least squares on LibriSpeech
# dev-clean vowels (tools/hiss_proto/vtemplate.py)
VT_A = np.array([-8.3, -15.4, -17.8, -21.0])
VT_C = np.array([-0.29, -0.33, -0.40, -0.44])
KLO, KHI = vl.KFREQ < 1000, vl.KFREQ > 2000


def stft(x):
    n = max(0, (len(x) - N) // H + 1)
    return np.fft.rfft(np.stack([x[i * H:i * H + N] * WIN for i in range(n)]), axis=1)


def istft(X, n):
    y = np.zeros(n + N)
    for i, f in enumerate(np.fft.irfft(X, N, axis=1) * WIN):
        y[i * H:i * H + N] += f
    return y[:n]


def bands_db(P, bands):
    return np.stack([10 * np.log10(P[:, (FR >= a) & (FR < b)].mean(1) + 1e-12) for a, b in bands], 1)


def model_env(m):
    """dB envelope of each decoded model frame at FB."""
    out = np.zeros((len(m), len(FB)))
    for k, r in enumerate(m):
        L = r["L"]
        f = np.arange(1, L + 1) * r["Wo"] * 4000 / np.pi
        out[k] = np.interp(FB, f, 20 * np.log10(r["A"][1:L + 1] + 1e-9))
    return out


def frame_map(m, bits, nfr):
    """Per output STFT frame: envelope features, voicing class (0 U, 1 V, 2 M), codec frame,
    and whether that codec frame is UU."""
    k = np.clip(np.arange(nfr) + HB_LAG, 0, len(m) - 1)
    E = model_env(m)[k]
    v = np.minimum(m["voiced"], 2)[k]
    code = bits[:, 0] * 2 + bits[:, 1]
    f = np.clip(np.arange(nfr) + HB_LAG, 0, 2 * len(code) - 1) // 2
    return E, v, f, code[f] == 0


def guess(W, E, v):
    """Per-class linear guess of the four high-band levels (dB) from the 0-4 kHz envelope."""
    ref = E[:, 6]
    F = np.column_stack([np.ones(len(E)), E[:, :6] - ref[:, None], ref])
    out = np.zeros((len(E), 4))
    for c in range(3):
        s = v == c
        out[s] = F[s] @ W[c] + ref[s, None]
    return out


def uu_residuals(hb, p, f, uu):
    """Mean (true - guess) over the STFT frames of each UU codec frame -> {frame: 4 dB}."""
    out = {}
    for g in np.unique(f[uu]):
        s = (f == g) & uu
        out[int(g)] = (hb[s] - p[s]).mean(0)
    return out


# vowel_shape="fine": ridge from the decoded 0-4 kHz shape (250 Hz bands, re 3.0-3.5 kHz)
# to 250 Hz bands at 4-8 kHz, plus a per-speaker shape (3 PCA coefficients of the speaker's
# mean miss, sent slowly). Trained on LibriSpeech dev-clean (tools/hiss_proto/vfine*.py).
FLO = [(f, f + 250) for f in range(250, 3750, 250)] + [(3750, 3950)]
FHI = [(f, f + 250) for f in range(4000, 8000, 250)]
FHC = np.array([a + 125.0 for a, _ in FHI])
_FINE = None


def fine_model():
    global _FINE
    if _FINE is None:
        _FINE = dict(np.load(os.path.join(HERE, "data", "hiss_vowel_fine.npz")))
    return _FINE


def fine_predict(P):
    """Per-frame 4-8 kHz prediction (dB re 3.0-3.5 kHz) and the reference level."""
    F = fine_model()
    ref = bands_db(P, [(3000, 3500)])[:, 0]
    X = bands_db(P, FLO) - ref[:, None]
    return np.column_stack([np.ones(len(X)), X]) @ F["W"], ref


def fine_levels(y16, nfr, spk_pc=None):
    F = fine_model()
    pr, ref = fine_predict(np.abs(stft(y16)[:nfr]) ** 2)
    if spk_pc is None:
        corr = F["m0"][None, :]
    else:
        pc = np.asarray(spk_pc, float)
        corr = F["m0"][None, :] + (pc @ F["Vt"] if pc.ndim == 2 else (F["Vt"].T @ pc)[None, :])
    return pr + ref[:, None] + corr


def fine_speaker(x16, y16, nfr):
    """Encoder side: the speaker's mean miss over vowel frames, as 3 PCA coefficients."""
    F = fine_model()
    P = np.abs(stft(x16)[:nfr]) ** 2
    lo = bands_db(P, [(0, 1000)])[:, 0]
    tot = 10 * np.log10(P.sum(1) + 1e-12)
    vow = (lo - bands_db(P, [(2500, 3000)])[:, 0] > 10) & (tot > np.percentile(tot, 60))
    Y = bands_db(P, FHI) - bands_db(P, [(3000, 3500)])[:, 0][:, None]
    pr, _ = fine_predict(np.abs(stft(y16)[:nfr]) ** 2)
    miss = (Y[vow] - pr[vow]).mean(0) - F["m0"]
    return F["Vt"] @ miss


def index_of(bits_row, nb=7):
    return int("".join(map(str, bits_row[2:2 + nb])), 2)


# ---------------------------------------------------------------- vowel side channel
WIN_FRAMES = 100              # one message per 2 s of frames (frame numbers known to both ends)
MSG_BITS = 16                 # 12 payload + CRC-4, one bit in each of the window's first 16 UU frames
BPOS = np.arange(4) - 1.5


def crc4(bits):
    r = 0
    for b in bits:                                  # x^4 + x + 1
        fb = ((r >> 3) & 1) ^ b
        r = ((r << 1) & 0xF) ^ (0x3 if fb else 0)
    return [(r >> (3 - i)) & 1 for i in range(4)]


def q_offset(r):
    """4 band offsets (dB) -> (level code 0..15: -10..20 dB in 2 dB; tilt code 0..3:
    -3/-1/1/3 dB per band)."""
    li = int(np.clip(np.round((r.mean() + 10) / 2), 0, 15))
    ti = int(np.clip(np.round((np.polyfit(BPOS, r, 1)[0] + 3) / 2), 0, 3))
    return li, ti


def offset_of(li, ti):
    return (-10 + 2 * li) + (-3 + 2 * ti) * BPOS


def pack_msg(codes):
    """[(liV, tiV), (liM, tiM)] -> 16 bits"""
    b = []
    for li, ti in codes:
        b += [(li >> (3 - i)) & 1 for i in range(4)] + [(ti >> (1 - i)) & 1 for i in range(2)]
    return b + crc4(b)


def unpack_msg(b):
    if len(b) < MSG_BITS or crc4(b[:12]) != list(b[12:16]):
        return None
    v = lambda x: int("".join(map(str, x)), 2)
    return [(v(b[0:4]), v(b[4:6])), (v(b[6:10]), v(b[10:12]))]


def running_pct(v, q, warm=100):
    """v[t] > the q-th percentile of v[:t+1], False during the first `warm` frames."""
    out = np.zeros(len(v), bool)
    for t in range(warm, len(v)):
        out[t] = v[t] > np.percentile(v[:t + 1], q)
    return out


PC_STEP = 4.0                    # speaker-shape coefficients: 4 bits each in 4 dB steps
PC_MIN = np.array([-52.0, -32.0, -32.0])   # ranges -52..8, -32..28, -32..28 (PC1 is mostly negative)


def pack_pc(pc):
    """3 speaker-shape coefficients (dB) -> 16 bits (3 x 4-bit codes + CRC-4)"""
    b = []
    for c, lo in zip(pc, PC_MIN):
        q = int(np.clip(np.round((c - lo) / PC_STEP), 0, 15))
        b += [(q >> (3 - i)) & 1 for i in range(4)]
    return b + crc4(b)


def unpack_pc(b):
    if len(b) < MSG_BITS or crc4(b[:12]) != list(b[12:16]):
        return None
    return np.array([PC_MIN[i] + PC_STEP * int("".join(map(str, b[4 * i:4 * i + 4])), 2) for i in range(3)])


class SottoV38(vl.SottoV37):
    """See the module docstring. encode() and decode_audio16() work at 16 kHz; decode()
    returns the 8 kHz model like the other versions."""

    UU_WO_GUARD = True      # UU frames' pitch field is a hiss index: never interpolate from it


    def __init__(self, model=MODEL, vowel=None, voiced_trim=0.0, texture="noise", pulse=1.0, vowel_shape=False, smooth_ms=30.0, peak_hz=None, peak_db=9.0, peak_bw=250.0, peak_floor=4.0, spk_pc=None, blend=False, whisper=False, release_ms=20.0, vsmooth=False, keep_mixed=False, sib_db=None, sib_voiced=False, **kw):
        """vowel: None (no high band in voiced frames), "guess" (voiced frames at the guess
        - 3 dB) or "adaptive" (voiced and mixed frames at the guess + a per-speaker offset
        sent one bit per UU frame; the UU index is then 6 bits)."""
        super().__init__(**kw)
        self.vowel, self.voiced_trim, self.texture, self.pulse = vowel, voiced_trim, texture, pulse
        self.vowel_shape, self.smooth_ms = vowel_shape, smooth_ms
        self.peak_hz, self.peak_db, self.peak_bw, self.peak_floor = peak_hz, peak_db, peak_bw, peak_floor
        self.spk_pc = spk_pc                  # vowel_shape="fine": 3 speaker-shape coefficients (side channel)
        self.whisper = whisper                # encoder: send whispered vowels (mixed runs with no
                                              # real harmonics and a wandering pitch) as unvoiced
        self.sib_db = sib_db                  # encoder: M/edge frames with 4-8 kHz > 0-1 kHz + sib_db -> UU
        self.sib_voiced = sib_voiced          # encoder: the sibilant rule also recodes VV frames (z, zh)
        self.keep_mixed = keep_mixed          # blend: mixed frames keep the -3 dB guessed hiss
        self.release_ms = release_ms          # blend: consonant hiss hold (0 = none)
        self.vsmooth = vsmooth                # encoder: no V/M flutter or U blips inside voiced runs
        self.blend = blend                    # one layer: max(vowel, consonant) power, flickers bridged
        if model:                                       # None while training the guess
            z = np.load(model, allow_pickle=True)
            self.W, self.cb = z["W"].item(), z["cb"]
            if vowel in ("adaptive", "speaker"):
                self.cb = z["cb6"]
        self.nb = 6 if vowel in ("adaptive", "speaker") else 7

    # ---------------------------------------------------------------- decoder hooks
    def _model_hook(self, m, rk, en):
        u = np.where(m["voiced"] == 0)[0]
        if len(u):
            mu = vl.set_wo(m[u], np.full(len(u), W50), np.zeros(len(u), int))
            m[u] = vl.set_energy(vl.from_rate_k(mu, rk[u]), en[u])
            for k in u:
                if rk[k, KLO].mean() - rk[k, KHI].mean() < 0:
                    m[k]["A"] *= 10 ** (UV_GAIN / 20)
        return m

    # ---------------------------------------------------------------- encoder
    def encode(self, x16):
        bits, hb, m = self.encode_low(x16)
        E, v, f, uu = frame_map(m, bits, len(hb))
        p = guess(self.W, E, v)
        res = uu_residuals(hb, p, f, uu)
        code = bits[:, 0] * 2 + bits[:, 1]
        nb = self.nb
        for g in np.where(code == 0)[0]:
            i = int(np.argmin(((self.cb - res[g]) ** 2).sum(1))) if g in res else 0
            bits[g, 2:2 + nb] = [(i >> (nb - 1 - b)) & 1 for b in range(nb)]
        if self.vowel == "adaptive":
            self._side_channel(bits, hb, p, E, v, f)
        elif self.vowel == "speaker":
            self._side_pc(bits, x16)
        return bits

    def _low16(self, bits, n16):
        """The decoder's own 0-4 kHz output at 16 kHz (no high band), as both ends see it."""
        y8 = vl.synthesise(self.decode(bits))[vl.ar.CODEC_DELAY:]
        n8 = n16 // 2
        return resample_poly(np.pad(y8, (0, max(0, n8 - len(y8))))[:n8], 2, 1)

    def _side_pc(self, bits, x16):
        """vowel="speaker": window w carries the speaker shape (3 PCA coefficients of the
        mean miss of the vowel-following predictor, 4 bits each in 4 dB steps, + CRC-4)
        measured over the vowel frames of windows 0..w-1. Causal: no look-ahead."""
        F = fine_model()
        y16 = self._low16(bits, len(x16))
        nfr = max(0, (len(y16) - N) // H + 1)
        P = np.abs(stft(x16)[:nfr]) ** 2
        Y = bands_db(P, FHI) - bands_db(P, [(3000, 3500)])[:, 0][:, None]
        pr, _ = fine_predict(np.abs(stft(y16)[:nfr]) ** 2)
        lo = bands_db(P, [(0, 1000)])[:, 0]
        tot = 10 * np.log10(P.sum(1) + 1e-12)
        # loud vowel frames only (within 10 dB of the running peak), like the training set
        # frames above the running 60th percentile of frame energy (a 1 dB histogram in C),
        # after a 0.5 s warm-up (1 s cost every clip a whole window of lock-in time)
        vow = (lo - bands_db(P, [(2500, 3000)])[:, 0] > 10) & running_pct(tot, 60, warm=50)
        fr = np.clip(np.arange(nfr) + HB_LAG, 0, 2 * len(bits) - 1) // 2
        code = bits[:, 0] * 2 + bits[:, 1]
        acc, cnt = np.zeros(len(FHI)), 0
        for w in range((len(bits) + WIN_FRAMES - 1) // WIN_FRAMES):
            pc = F["Vt"] @ (acc / cnt - F["m0"]) if cnt >= 20 else np.zeros(3)
            uuf = [g for g in range(w * WIN_FRAMES, min(len(bits), (w + 1) * WIN_FRAMES)) if code[g] == 0]
            for g, b in zip(uuf, pack_pc(pc)):
                bits[g, 8] = b
            ts = np.where((fr >= w * WIN_FRAMES) & (fr < (w + 1) * WIN_FRAMES) & vow)[0]
            acc += (Y[ts] - pr[ts]).sum(0)
            cnt += len(ts)

    def _read_pc(self, bits, good):
        """-> per codec frame the speaker shape in force. A window's message applies from
        the frame that completes it (its 16th unvoiced frame), not from the next window; a
        lost frame before that drops the window's message (a lost frame may have carried a
        bit); lost frames after it do no harm. Starts at the mean speaker."""
        cur, out = np.zeros(3), np.zeros((len(bits), 3))
        for g0 in range(0, len(bits), WIN_FRAMES):
            got, ok = [], True
            for g in range(g0, min(len(bits), g0 + WIN_FRAMES)):
                out[g] = cur
                if not ok or len(got) == MSG_BITS:
                    continue
                if not good[g]:
                    ok = False
                elif bits[g, 0] == 0 and bits[g, 1] == 0:
                    got.append(int(bits[g, 8]))
                    if len(got) == MSG_BITS:
                        msg = unpack_pc(got)
                        if msg is not None:
                            cur = msg
                            out[g] = cur
        return out

    def _side_channel(self, bits, hb, p, E, v, f):
        """Window w carries the running mean of (true - guess) over the voiced and mixed
        frames of windows 0..w-1, so the encoder needs no look-ahead."""
        code = bits[:, 0] * 2 + bits[:, 1]
        live = E[:, 6] > np.maximum.accumulate(E[:, 6]) - 35
        acc = {1: [], 2: []}
        for w in range((len(bits) + WIN_FRAMES - 1) // WIN_FRAMES):
            uuf = [g for g in range(w * WIN_FRAMES, min(len(bits), (w + 1) * WIN_FRAMES)) if code[g] == 0]
            codes = [q_offset(np.mean(acc[c], 0)) if acc[c] else (5, 1) for c in (1, 2)]  # (5,1): 0 dB, -1/band
            for g, b in zip(uuf, pack_msg(codes)):
                bits[g, 8] = b
            ts = np.where((f >= w * WIN_FRAMES) & (f < (w + 1) * WIN_FRAMES))[0]
            for t in ts:
                if v[t] in (1, 2) and live[t]:
                    acc[v[t]].append(hb[t] - p[t])
    def encode_low(self, x16):
        """-> (bits without the high-band index, true band levels per STFT frame, the
        encoder's own decoded model)."""
        x8 = resample_poly(x16, 1, 2)
        bits = vl.SottoV37.encode(self, x8)
        self._onsets(x8, bits)
        if self.whisper:
            self._whisper(x8, bits)
        if self.vsmooth:
            self._vsmooth(bits)
        P = np.abs(stft(x16)) ** 2
        hb = bands_db(P, HB)
        m = self.decode(bits)
        _, _, f, _ = frame_map(m, bits, len(P))
        top, low = bands_db(P, [(4000, 8000)])[:, 0], bands_db(P, [(0, 3900)])[:, 0]
        for t in np.where((top[1:] - top[:-1] > BURST_DB) & (top[1:] > low[1:] - 15))[0] + 1:
            bits[f[t], 0] = bits[f[t], 1] = 0            # burst: send as UU
        if self.sib_db is not None:
            # sibilant: an s/sh that starts inside a mixed run keeps the vowel's pitch for
            # a few frames (a warble under the hiss, and no high-band index). If the 4-8 kHz
            # band is louder than 0-1 kHz by sib_db in either 10 ms half, send it as UU.
            one = bands_db(P, [(0, 1000)])[:, 0]
            code = bits[:, 0] * 2 + bits[:, 1]
            codes = (1, 2, 3) if self.sib_voiced else (2, 3)
            for t in np.where(top - one > self.sib_db)[0]:
                g = f[t]
                if g < len(bits) and code[g] in codes:
                    bits[g, 0] = bits[g, 1] = 0
        return bits, hb, self.decode(bits)

    @staticmethod
    def _vsmooth(bits, max_blip=2, win=7, switches=3):
        """Voiced fricatives ('zh' in 'division') make v3.7 flutter V M V M and drop in
        U-edge blips (M e e M), heard as a stutter. Inside a voiced run: edge runs of
        <= `max_blip` frames between voiced/mixed frames become M, and frames whose
        `win`-frame neighbourhood switches V<->M >= `switches` times become M."""
        code = bits[:, 0] * 2 + bits[:, 1]
        n = len(code)
        g = 0
        while g < n:
            if code[g] != 3:
                g += 1
                continue
            h = g
            while h < n and code[h] == 3:
                h += 1
            if g > 0 and h < n and h - g <= max_blip and code[g - 1] in (1, 2) and code[h] in (1, 2):
                code[g:h] = 2
            g = h
        c0 = code.copy()
        for g in range(n):
            if c0[g] not in (1, 2):
                continue
            w = c0[max(0, g - win // 2):g + win // 2 + 1]
            w = w[w > 0]
            if np.sum((w[1:] != w[:-1]) & (w[1:] < 3) & (w[:-1] < 3)) >= switches:
                code[g] = 2
        bits[:, 0], bits[:, 1] = code >> 1, code & 1

    def _features(self, x8):
        """Per 10 ms analysis frame: mean harmonic SNR below 1 kHz of the tracked pitch
        (nan when unvoiced) and the pitch jitter |d log Wo|. Cached per input."""
        key = (len(x8), float(x8[:4000].sum()), float(x8[-4000:].sum()))
        if getattr(self, "_feat_key", None) == key:
            return self._feat
        import sinusoid as sn
        m = vl.analyse(x8, self.PITCH)
        Sw = sn.spectra(np.round(x8 * 32767))
        n = min(len(m), len(Sw))
        snr = np.full(n, np.nan)
        for k in range(n):
            if m["voiced"][k] > 0:
                wo = m["Wo"][k]
                L = sn.n_harm(wo)
                fh = np.arange(L + 1) * wo * 8000 / (2 * np.pi)
                snr[k] = sn.harmonic_snr(Sw[k], wo, L)[(fh > 0) & (fh < 1000)].mean()
        jt = np.r_[0.0, np.abs(np.diff(np.log(m["Wo"][:n])))]
        self._feat_key, self._feat = key, (snr, jt)
        return self._feat

    def _whisper(self, x8, bits, min_frames=10, snr_db=6.0, jit=0.025):
        """A whispered vowel has no glottal pulses, but the tracker still finds a 'pitch' and
        v3.7 sends it as mixed: harmonics below 1 kHz at a wandering pitch, heard as a low
        warble. Runs of >= `min_frames` mixed/edge frames whose median harmonic SNR below
        1 kHz is < `snr_db` and whose pitch moves > `jit` (log ratio per 10 ms, median) are
        sent as UU instead. Real voiced runs: SNR ~11-16 dB, jitter ~0.003-0.016."""
        snr, jt = self._features(x8)
        n = len(snr)
        code = bits[:, 0] * 2 + bits[:, 1]
        g = 0
        while g < len(bits):
            if code[g] not in (2, 3):
                g += 1
                continue
            h = g
            while h < len(bits) and code[h] in (2, 3):
                h += 1
            if h - g >= min_frames:
                sl = slice(2 * g, min(2 * h, n))
                s_, j_ = snr[sl], jt[sl]
                if np.nanmedian(s_) < snr_db and np.median(j_) > jit:
                    bits[g:h, 0] = bits[g:h, 1] = 0
            g = h

    def _onsets(self, x8, bits, loud_db=10.0, lowdom=3.0):
        m = vl.analyse(x8, self.PITCH)
        if len(m) % 2:
            m = np.concatenate([m, m[-1:]])
        rk = vl.to_rate_k(m)
        en = rk.mean(1)
        # causal loudness reference (for a streaming encoder): a peak follower that decays
        # 0.05 dB per 10 ms, standing in for the clip's 90th percentile
        ref, pk = np.zeros(len(en)), -1e9
        for k, e in enumerate(en):
            pk = max(e, pk - 0.05)
            ref[k] = pk - 6.0
        loud = en > ref - loud_db
        code = bits[:, 0] * 2 + bits[:, 1]
        for g in range(len(bits)):
            k = 2 * g
            if code[g] or not loud[k] or rk[k, KLO].mean() - rk[k, KHI].mean() <= lowdom:
                continue
            near = [h for d in (1, -1, 2, -2, 3, -3) for h in [g + d]
                    if 0 <= h < len(bits) and code[h] in (1, 2)]
            if near:
                bits[g, 0], bits[g, 1] = 1, 0           # MM, with the neighbour's pitch
                bits[g, 2:9] = bits[near[0], 2:9]

    def _read_side(self, bits, good):
        """-> per window {1: V offset, 2: M offset}; a message (window w) applies from
        window w + 1. Any lost frame in a window drops its message."""
        nw = (len(bits) + WIN_FRAMES - 1) // WIN_FRAMES
        cur = {1: offset_of(5, 1), 2: offset_of(5, 1)}
        out = []
        for w in range(nw + 1):
            out.append(dict(cur))
            g0, g1 = w * WIN_FRAMES, min(len(bits), (w + 1) * WIN_FRAMES)
            if g0 >= len(bits) or not np.all(good[g0:g1]):
                continue
            seg = bits[g0:g1]
            msg = unpack_msg([int(r[8]) for r in seg if r[0] == 0 and r[1] == 0][:MSG_BITS])
            if msg:
                cur = {1: offset_of(*msg[0]), 2: offset_of(*msg[1])}
        return out

    # ---------------------------------------------------------------- 16 kHz decoder
    def decode_audio16(self, bits, good=None, n16=None, seed=0):
        bits = np.asarray(bits)
        good = np.ones(len(bits), bool) if good is None else np.asarray(good, bool)
        b2, last, run, r = bits.copy(), None, np.zeros(len(bits), int), 0
        for g in range(len(bits)):                      # the bits concealment decodes
            r = 0 if good[g] else r + 1
            run[g] = r
            if good[g]:
                last = bits[g].copy()
            elif last is not None:
                b2[g] = last
        m = self.decode(bits, good)
        y8 = vl.synthesise(m)[vl.ar.CODEC_DELAY:]
        n8 = n16 // 2 if n16 else len(y8)
        y16 = resample_poly(np.pad(y8, (0, max(0, n8 - len(y8))))[:n8], 2, 1)
        nfr = max(0, (len(y16) - N) // H + 1)
        E, v, f, uu = frame_map(m, b2, nfr)
        p = guess(self.W, E, v)
        code = b2[:, 0] * 2 + b2[:, 1]
        off = self._read_side(bits, good) if self.vowel == "adaptive" else None
        if self.vowel_shape in ("template", "smooth") and nfr:
            Ld = bands_db(np.abs(stft(y16)[:nfr]) ** 2, [(2000, 2500), (2500, 3000), (3000, 3500)])
            sl = np.clip(np.polyfit(np.array([2.25, 2.75, 3.25]), Ld.T, 1)[0], -30, 10)
            vt = Ld[:, 2:3] + VT_A + VT_C * sl[:, None]
        if self.vowel_shape == "fine" and nfr:
            pcs = self.spk_pc
            if self.vowel == "speaker":                 # from the side channel, per frame
                win = self._read_pc(bits, good)
                pcs = win[np.minimum(f, len(win) - 1)]
            vt = fine_levels(y16, nfr, pcs)             # 16 bands of 250 Hz, not 4
        mask = np.zeros(nfr)
        for t in range(nfr):
            src = f[t] if uu[t] else None
            if src is None and v[t] == 0:               # unvoiced half of an edge frame
                src = next((g for g in (f[t] - 1, f[t] + 1) if 0 <= g < len(code) and code[g] == 0), None)
            if src is not None:
                p[t] += self.cb[index_of(b2[src], self.nb)]
                mask[t] = 1
            elif self.vowel == "adaptive" and v[t] in (1, 2):
                o = off[f[t] // WIN_FRAMES][v[t]]
                if v[t] == 1 and self.vowel_shape == "template":
                    p[t] = vt[t]
                elif v[t] == 1 and self.vowel_shape:
                    # follow the vowel: start at the guessed 4-5 kHz level and continue the
                    # envelope's own slope from 2.75 to 3.8 kHz (clamped to -14..-3 dB/kHz)
                    slope = np.clip((E[t, 6] - E[t, 3]) / 1.05, -14.0, -3.0)
                    p[t] = p[t, 0] + o.mean() + slope * (HBC - HBC[0]) / 1000
                elif v[t] == 1 and getattr(self, "lvl_only", False):
                    p[t] += o.mean()                    # level only: keep the guess's shape
                else:
                    p[t] += o
                p[t] += self.voiced_trim if v[t] == 1 else 0.0
                mask[t] = 2
            elif v[t] == 2 or (self.vowel == "guess" and v[t] == 1):
                p[t] += MIXED_DB
                mask[t] = 3
        fade = np.array([0.0 if run[g] == 0 else -80.0 if run[g] > 4 else -6.0 * max(0, run[g] - 2)
                         for g in f])
        p += fade[:, None] + np.where(mask == 1, UV_GAIN, 0.0)[:, None]   # the boost matches the 0-4 kHz one
        lvl = np.stack([np.interp(FR, HBC, h) for h in p]) if nfr else np.zeros((0, len(FR)))
        mag = np.sqrt(10 ** (lvl / 10)) * np.clip((FR - 3900) / 200, 0, 1) * mask[:, None]
        ph = np.exp(2j * np.pi * np.random.default_rng(seed).random(mag.shape))
        X = mag * ph
        if self.vowel_shape in ("smooth", "fine") and nfr:
            # one continuous vowel layer under the consonant hiss: the template level for
            # every frame, smoothed in time (one pole, ~30 ms) and faded in and out by a
            # smoothed voiced/mixed weight, so it never switches on or off in one frame
            # drop the per-frame vowel hiss; with blend, mixed frames (z, zh, v: voiced
            # fricatives) keep their guessed hiss in the consonant layer
            X[(mask == 2) if self.blend and self.keep_mixed else (mask >= 2)] = 0
            a = np.exp(-10.0 / self.smooth_ms)
            lv, wv = vt.copy(), np.zeros(nfr)
            want = ((v == 1) | (v == 2)) & (mask != 1)
            if self.blend:
                # a short unvoiced flicker (<= 40 ms) inside a vowel keeps the vowel layer on
                w0 = want.copy()
                for g0 in range(1, nfr):
                    if w0[g0] or not w0[g0 - 1]:
                        continue                        # only at the start of a gap
                    g1 = g0
                    while g1 < nfr and not w0[g1]:
                        g1 += 1
                    if g1 < nfr and g1 - g0 <= 4:
                        want[g0:g1] = True
            w = 0.0
            for t in range(nfr):
                if t: lv[t] = a * lv[t - 1] + (1 - a) * vt[t]
                w = a * w + (1 - a) * float(want[t])
                wv[t] = w
            lv += fade[:, None] + self.voiced_trim
            lvl2 = np.stack([np.interp(FR, FHC if self.vowel_shape == "fine" else HBC, h) for h in lv])
            if self.peak_hz:
                # a whispered vowel keeps a resonance up here (Jeff's bench recording: one
                # peak per vowel, 4.9-7.3 kHz): add it, Gaussian in dB, on a lower floor
                pk = self.peak_db * np.exp(-0.5 * ((FR - self.peak_hz) / self.peak_bw) ** 2)
                lvl2 = lvl2 - self.peak_floor + pk[None, :]
            mag2 = np.sqrt(10 ** (lvl2 / 10)) * np.clip((FR - 3900) / 200, 0, 1) * wv[:, None]
            ph2 = np.exp(2j * np.pi * np.random.default_rng(seed + 1).random(mag2.shape))
            if self.blend:
                # one hiss layer: per bin, the louder of the vowel layer and the consonant hiss,
                # the consonant part held with a 20 ms release so it never drops in one frame
                pc = np.abs(X) ** 2
                if self.release_ms > 0:
                    ar = np.exp(-10.0 / self.release_ms)
                    for t in range(1, nfr):
                        pc[t] = np.maximum(pc[t], ar * pc[t - 1])
                X = np.sqrt(np.maximum(pc, mag2 ** 2)) * ph2
            else:
                X = X + mag2 * ph2
        if self.texture == "pulsed" and nfr:
            # voiced/mixed frames: noise shaped in time by the pitch pulses of the decoded
            # 2-3.8 kHz band (real voiced high band is pulsed; steady noise sounds scratchy)
            from scipy.signal import butter, sosfiltfilt
            sel = (mask == 2)[:, None]
            hu = istft(np.where(sel, 0, X), len(y16))
            hv = istft(np.where(sel, X, 0), len(y16))
            env = np.abs(sosfiltfilt(butter(4, [2000, 3800], "band", fs=16000, output="sos"), y16))
            env = np.convolve(env, np.ones(16) / 16, "same")          # 1 ms
            rms = np.sqrt(np.convolve(env ** 2, np.ones(320) / 320, "same")) + 1e-9
            return y16 + hu + hv * (env / rms) ** self.pulse
        return y16 + istft(X, len(y16))
