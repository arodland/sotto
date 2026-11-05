"""Modulators and a demodulator for the K6 RF chip's FSK packet modem, sent through an
ordinary FM transmitter (the IC-7100) fed from a sound card.

Profile D (primary): direct NRZ 2-FSK. The audio *is* the frequency shift, so it needs a
flat modulation input (the IC-7100's DATA jack in 9600 bps mode). Bit 1 = positive audio =
+deviation (assumed; `invert` flips it).

Profile F (fallback): FFSK, phase-continuous 1200 Hz / 1800 Hz tones at 1200 baud, bit 1 =
1200 Hz (MDC1200 convention, assumed). Survives a voice audio path (USB audio, pre-emphasis,
300 Hz high-pass). At 2400 baud profile F uses 1200 / 2400 Hz tones (the chip's FFSK 1200/2400
mode with its baud register doubled): half a cycle of 1200 Hz or a whole one of 2400 Hz per
bit, which is MSK, the same modulation index as 1200/1800 at 1200 baud.

Channel models a voice transmitter's audio path: optional +6 dB/octave pre-emphasis, a
high-pass and a low-pass. It is used to simulate, and `Channel.inverse` pre-compensates
profile D when only a voice path is available (an experiment: a high-pass can't be undone
down to DC, so long runs still droop).

Demodulator mimics a simple chip receiver: low-pass, correlation against the end of the
preamble plus the sync word, a slicer whose threshold follows the local mean (the whitened
payload is balanced). Profile F audio is first turned into an NRZ-like signal by measuring
the tone's instantaneous frequency. It is a test bench, not a copy of the chip.
"""
from dataclasses import dataclass
from math import gcd

import numpy as np
from scipy import signal

FS = 48000
MARK, SPACE = 1200.0, 1800.0          # profile F: bit 1, bit 0


def ffsk_tones(baud=1200):
    """Profile F's (bit 1, bit 0) tones: 1200/1800 Hz at 1200 baud, 1200/2400 Hz at 2400."""
    return (MARK, 2400.0) if baud >= 2400 else (MARK, SPACE)


@dataclass
class Channel:
    """Audio path of a voice FM transmitter. pre_emph: corner in Hz of +6 dB/octave
    pre-emphasis up to emph_top (None: flat), normalised to 0 dB at 1 kHz. hpf / lpf:
    Butterworth corners in Hz (None: off). limit: deviation limiter, as a multiple of the
    1 kHz gain applied to a full-scale input (None: off)."""
    pre_emph: float = None
    emph_top: float = 3000.0
    hpf: float = 300.0
    hpf_order: int = 2
    lpf: float = 3000.0
    lpf_order: int = 4
    limit: float = None

    def response(self, f):
        f = np.asarray(f, float)
        h = np.ones(f.shape, complex)
        if self.pre_emph:
            def e(x):
                return (1 + 1j * x / self.pre_emph) / (1 + 1j * x / self.emph_top)
            h *= e(f) / abs(e(1000.0))
        for fc, order, kind in ((self.hpf, self.hpf_order, "highpass"),
                                (self.lpf, self.lpf_order, "lowpass")):
            if fc:
                b, a = signal.butter(order, 2 * np.pi * fc, kind, analog=True)
                h *= signal.freqs(b, a, 2 * np.pi * f)[1]
        return h

    def apply(self, x, fs=FS):
        y = _fft_filter(x, fs, self.response)
        return np.clip(y, -self.limit, self.limit) if self.limit else y

    def inverse(self, x, fs=FS, max_gain_db=30.0):
        return precompensate(x, self.response, fs, max_gain_db)


def precompensate(x, response, fs=FS, max_gain_db=30.0):
    """Filter x by the inverse of response(f) (complex), so that the channel undoes it. The
    inverse's gain is capped at max_gain_db above its 1 kHz value, phase kept: a high-pass
    can't be undone all the way to DC."""
    ref = 1 / abs(response(np.array([1000.0]))[0])
    cap = ref * 10 ** (max_gain_db / 20)

    def inv(f):
        h = response(f)
        g = 1 / np.where(abs(h) < 1e-12, 1e-12, h)
        return np.minimum(abs(g), cap) * np.exp(1j * np.angle(g))
    return _fft_filter(x, fs, inv)


def _fft_filter(x, fs, resp):
    x = np.asarray(x, float)
    n = 1 << int(np.ceil(np.log2(len(x) + fs // 2)))
    f = np.fft.rfftfreq(n, 1 / fs)
    h = resp(np.maximum(f, 1e-3))
    return np.fft.irfft(np.fft.rfft(x, n) * h, n)[:len(x)]


def resample(x, fs_in, fs_out):
    x = np.asarray(x, float)
    if fs_in == fs_out:
        return x
    g = gcd(int(fs_in), int(fs_out))
    return signal.resample_poly(x, int(fs_out) // g, int(fs_in) // g)


def gaussian_taps(sps, bt):
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    s = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    h = np.exp(-t ** 2 / (2 * s ** 2))
    return h / h.sum()


def _hold(bits, baud, fs):
    sps = fs / baud
    n = int(round(len(bits) * sps))
    return np.asarray(bits)[np.minimum((np.arange(n) / sps).astype(int), len(bits) - 1)]


def nrz(bits, baud=1200, fs=FS, bt=0.5):
    """bits -> NRZ +-1, Gaussian-shaped (bt=None: square)."""
    x = 2.0 * _hold(bits, baud, fs) - 1
    if bt:
        x = np.convolve(x, gaussian_taps(int(round(fs / baud)), bt), "same")
    return x


def ffsk(bits, baud=1200, fs=FS, mark=MARK, space=SPACE):
    """bits -> phase-continuous tones, bit 1 = mark (1200 Hz)."""
    f = np.where(_hold(bits, baud, fs) > 0, mark, space)
    return np.sin(2 * np.pi * np.cumsum(f) / fs)


def _ramp(x, fs, ms=5.0):
    r = int(fs * ms / 1000)
    if r and len(x) > 2 * r:
        w = 0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)
        x[:r] *= w
        x[-r:] *= w[::-1]
    return x


def modulate(bits, profile="D", baud=1200, fs=FS, level=0.5, invert=False, bt=0.5,
             comp=None, max_gain_db=30.0, tones=None):
    """bits -> audio in -1..1 for the transmitter's modulation input; `level` is its peak.

    comp: anything with a response(f) method (a Channel, or cal.Measured from a calibration
    recording), whose inverse is applied first. Profile D: undoes a voice path's filters as
    far as max_gain_db allows. Profile F: undoes pre-emphasis, so both tones reach the same
    deviation (optional; the chip's FFSK receiver may not care). tones: profile F's (bit 1,
    bit 0) Hz instead of ffsk_tones(baud), to try a tone pair at another baud rate."""
    if invert:
        bits = 1 - np.asarray(bits)
    x = nrz(bits, baud, fs, bt) if profile == "D" else ffsk(bits, baud, fs, *(tones or ffsk_tones(baud)))
    if comp is not None:
        x = precompensate(x, comp.response, fs, max_gain_db)
    x = x / max(np.max(np.abs(x)), 1e-9) * level
    return _ramp(x, fs)


def bits_of(data):
    return np.unpackbits(np.frombuffer(bytes(data), np.uint8))


def tone_freq(x, fs=FS, baud=1200, tones=None):
    """Profile F audio -> instantaneous tone frequency (Hz), smoothed over about a bit."""
    t = tones or ffsk_tones(baud)
    # the passband widens with the baud rate: 1200/1800 tones at 2400 baud through the
    # 1200-baud band (700 Hz to 2.6 kHz) gave 1.5% bit errors with no noise at all
    lo, hi = max(200.0, min(t) - 0.4 * baud), max(t) + 0.6 * baud
    b = signal.firwin(int(fs / 300) | 1, [lo, hi], pass_zero=False, fs=fs)
    a = signal.hilbert(signal.filtfilt(b, [1.0], x))
    d = np.angle(a[1:] * np.conj(a[:-1])) * fs / (2 * np.pi)
    return np.concatenate([d[:1], d])


class Demodulator:
    """Audio -> list of (sample index, payload bytes, inverted?, amplitude).

    For profile D the input is discriminator audio (the receiver's flat data output, or the
    modulator's own output); for profile F it is the received tones. `payload` is 64 for
    profile L and 16 for profile S."""

    def __init__(self, profile="D", baud=1200, fs=FS, payload=64, threshold=0.7, air_sync=None,
                 stream=False, tones=None):
        from .pager import PREAMBLE_BYTE, SYNC
        self.profile, self.baud, self.fs, self.thr, self.payload = profile, baud, fs, threshold, payload
        self.sps = fs / baud
        self.mark, self.space = tones or ffsk_tones(baud)
        self.tones = tones
        self.lp = signal.firwin(2 * int(round(self.sps)) + 1, 0.75 * baud, fs=fs)
        # stream framing: no preamble byte before each sync, only the last payload's end
        ref_bits = bits_of((b"" if stream else bytes([PREAMBLE_BYTE])) + (air_sync or SYNC))
        self.ref = nrz(ref_bits, baud, fs, 0.5)
        self.ref -= self.ref.mean()
        self.ref_bits = len(ref_bits)
        self.known = ref_bits

    def baseband(self, x):
        """-> NRZ-like signal, positive = bit 1 (before any inversion)."""
        x = np.asarray(x, float)
        if self.profile == "F":
            x = (0.5 * (self.mark + self.space) - tone_freq(x, self.fs, self.baud, self.tones)) / (0.5 * (self.space - self.mark))
        return signal.filtfilt(self.lp, [1.0], x)

    def correlate(self, y):
        r = self.ref
        w = int(round(16 * self.sps)) | 1
        z = y - signal.convolve(y, np.ones(w) / w, "same")
        num = signal.correlate(z, r, "valid")
        e = np.sqrt(np.maximum(signal.correlate(z ** 2, np.ones(len(r)), "valid"), 0))
        # an energy floor, so near-silence (or a carrier with no data) can't correlate
        floor = 0.2 * np.sqrt(np.mean(z ** 2) * len(r))
        return num / (np.maximum(e, floor) * np.linalg.norm(r) + 1e-30)

    def _slice(self, y, j, idx, alpha=0.2):
        """Decision-directed slicer. The 'one' and 'zero' levels start from the preamble and
        sync (known bits) and track with a ~5-bit time constant; the threshold is midway.
        This follows baseline wander and level changes without being pulled by runs."""
        known = self.known
        kt = np.round(j + self.sps / 2 + np.arange(len(known)) * self.sps).astype(int)
        ky = y[kt]
        hi, lo = ky[known == 1].mean(), ky[known == 0].mean()
        self.last_amp = 0.5 * (hi - lo)
        v = y[idx]
        b = np.zeros(len(v), np.uint8)
        for n, s in enumerate(v):
            one = s > 0.5 * (hi + lo)
            b[n] = one
            if one:
                hi += alpha * (s - hi)
            else:
                lo += alpha * (s - lo)
        return b

    def demodulate(self, x):
        y = self.baseband(x)
        c = self.correlate(y)
        mag = np.abs(c)
        nbits = 8 * self.payload
        look = int(16 * self.sps)
        out, i = [], 0
        while i < len(mag):
            if mag[i] < self.thr:
                i += 1
                continue
            j = i + int(np.argmax(mag[i:i + look]))
            inv = bool(c[j] < 0)
            t = j + self.ref_bits * self.sps + self.sps / 2 + np.arange(nbits) * self.sps
            idx = np.round(t).astype(int)
            if idx[-1] >= len(y):
                break
            b = self._slice(-y if inv else y, j, idx)
            out.append((int(j), np.packbits(b).tobytes(), inv, self.last_amp))
            i = int(idx[-1])
        return out


def receive(x, fs, profile="D", baud=1200, payload_profile=None, invert=False, stream=False,
            tones=None):
    """Audio at any rate -> (Assembler with the messages, list of (start time s, CRC ok,
    inverted, amplitude)). The amplitude is half the eye opening measured on the preamble
    and sync, in input units (Hz for IQ input); for profile F it is relative to the tone
    shift. Two transmitters recorded on the same receiver have equal deviation when their
    amplitudes match."""
    from .pager import L, Assembler
    pp = payload_profile or L
    d = Demodulator(profile, baud, FS, pp.payload, stream=stream, tones=tones)
    y = resample(x, fs, FS)
    a = Assembler(pp)
    times = []
    for start, p, inv, amp in d.demodulate(-y if invert else y):
        q = a.push(p)
        times.append((start / FS, q is not None, inv, amp))
    return a, times
