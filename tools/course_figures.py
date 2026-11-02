"""Make the figures and listening demos for the course in docs/course/.

Every plot is drawn from real speech in docs/samples/ and from the real sotto code
(sim/vocoder_lab.py, modem/). Needs the Codec 2 build (tools/build_codec2.sh) and
numpy, scipy, soundfile, matplotlib. pesq is not needed.

The hiss-band figure and clips (chapter 8) also need the v3.8 C tools: `make -C c`.
`--hiss-only` makes just those.

Usage: python tools/course_figures.py [--out docs/course] [--hiss-only]
"""
import argparse, os, subprocess, sys, tempfile, types
import numpy as np
import soundfile as sf
from scipy import signal
from scipy.signal import resample_poly
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, ROOT)
try:
    import pesq  # noqa: F401  (audio_race imports it; the figures don't score anything)
except ImportError:
    sys.modules["pesq"] = types.SimpleNamespace(pesq=None)
import vocoder_lab as vl
import c4fm_sim as cs
from audio_io import write_flac
from modem import c4fm, fec, payload
from modem.link import Transmitter

SAMPLES = os.path.join(ROOT, "docs", "samples")
FS = 8000
MALE, FEMALE = "male_ve9qrp_15s", "female_libri198_14s"
CODEC = vl.SottoV35            # the version the course describes
PITCH = CODEC.PITCH

# Reference palette (light mode), see the figures' README
BG, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, AQUA, YELLOW, VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#6250d6"
SPEC = LinearSegmentedColormap.from_list("spec", [BG, "#9cc3ef", BLUE, "#0d2c52"])

plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.titlecolor": INK, "axes.titlesize": 12, "axes.titleweight": "bold",
    "axes.titlelocation": "left", "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10.5,
    "font.family": "DejaVu Sans", "lines.linewidth": 2, "legend.frameon": False,
    "svg.fonttype": "none", "svg.hashsalt": "sotto-course",
})


def save(fig, out, name):
    fig.savefig(os.path.join(out, "figures", name), format="svg", bbox_inches="tight",
                metadata={"Date": None})
    plt.close(fig)
    print("wrote", name)


def load(name, sub=""):
    x, fs = sf.read(os.path.join(SAMPLES, sub, name + ".flac"))
    assert fs == FS
    return x


def centre(i):
    """Sample at the centre of c2sim's analysis window for 10 ms frame i."""
    return (i - 1) * 80


def pick_voiced(m, hi):
    """A loud, steady voiced frame at a typical pitch: voiced for +-3 frames, pitch steady."""
    e = vl.frame_energy_db(m)
    med = np.median(m["Wo"][m["voiced"] > 0])
    best = None
    for i in range(4, hi - 4):
        w = m[i - 3:i + 4]
        if (w["voiced"] > 0).all() and np.ptp(w["Wo"]) < 0.03 * m[i]["Wo"] \
                and abs(m[i]["Wo"] / med - 1) < 0.15:
            if best is None or e[i] > e[best]:
                best = i
    return best


def pick_unvoiced(m, hi):
    e = vl.frame_energy_db(m)
    cand = [i for i in range(4, hi - 4) if (m[i - 2:i + 3]["voiced"] == 0).all()]
    return max(cand, key=lambda i: e[i])


def harmonic_fit(seg, wo, L):
    """Least-squares fit of L harmonics of wo to seg -> (amplitude, phase) per harmonic."""
    n = np.arange(len(seg)) - len(seg) // 2
    cols = []
    for k in range(1, L + 1):
        cols += [np.cos(k * wo * n), np.sin(k * wo * n)]
    X = np.array(cols).T
    c, *_ = np.linalg.lstsq(X, seg, rcond=None)
    a, b = c[0::2], c[1::2]
    return np.hypot(a, b), np.arctan2(-b, a), n


def lpc_envelope(frame, order, f):
    """LPC (autocorrelation method) envelope in dB at frequencies f."""
    w = frame * np.hanning(len(frame))
    r = np.correlate(w, w, "full")[len(w) - 1:len(w) + order]
    a = np.concatenate([[1.0], np.linalg.solve(
        np.array([[r[abs(i - j)] for j in range(order)] for i in range(order)]), -r[1:order + 1])])
    _, h = signal.freqz([1.0], a, worN=2 * np.pi * f / FS)
    return 20 * np.log10(np.abs(h) + 1e-12)


def spectrogram(ax, x, t0, t1, vmax_db=None):
    f, t, S = signal.spectrogram(x[int(t0 * FS):int(t1 * FS)], FS, window="hann",
                                 nperseg=256, noverlap=224, nfft=512)
    S = 10 * np.log10(S + 1e-12)
    top = vmax_db if vmax_db is not None else S.max()
    ax.pcolormesh(t, f, S, cmap=SPEC, vmin=top - 70, vmax=top, shading="auto", rasterized=True)
    ax.set_ylim(0, 4000)
    ax.grid(False)
    return top


# ---------------------------------------------------------------- figures
def fig_waveform(out, x, m):
    iv, iu = pick_voiced(m, 390), pick_unvoiced(m, 390)
    t = np.arange(len(x)) / FS
    fig = plt.figure(figsize=(9, 5.4))
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.15], hspace=0.45, wspace=0.18)
    ax = fig.add_subplot(gs[0, :])
    blk = x[:4 * FS].reshape(-1, 16)                 # min/max per 2 ms keeps the file small
    ax.fill_between(t[:4 * FS:16], blk.min(1), blk.max(1), color=INK2, lw=0)
    ax.set_title("4 seconds of a man talking: the microphone signal")
    ax.set_xlabel("time (seconds)")
    ax.set_ylabel("air pressure")
    ax.set_yticks([0])
    for i, col, lab in ((iv, BLUE, "voiced"), (iu, ORANGE, "unvoiced")):
        c = centre(i) / FS
        if c < 4:
            ax.axvspan(c - 0.02, c + 0.02, color=col, alpha=0.25, lw=0)
            ax.text(c + (0.03 if lab == "voiced" else -0.03), ax.get_ylim()[0] * 0.85, lab, color=col,
                    ha="left" if lab == "voiced" else "right", va="bottom",
                    fontsize=10, fontweight="bold")
    for k, (i, col, title) in enumerate(((iv, BLUE, "Voiced (a vowel): it repeats"),
                                         (iu, ORANGE, "Unvoiced (a hiss like \"s\"): no pattern"))):
        a = fig.add_subplot(gs[1, k])
        c = centre(i)
        s = x[c - 160:c + 160]
        tt = (np.arange(320) - 160) / FS * 1000
        a.plot(tt, s, color=col, lw=1.4)
        a.set_title(title, fontsize=10.5)
        a.set_xlabel("time (milliseconds)")
        a.set_yticks([0])
        if k == 0:
            P = 2 * np.pi / m[i]["Wo"] / FS * 1000
            y = s.max() * 1.08
            for j in range(-2, 2):
                a.axvline(j * P, color=INK2, lw=0.8, ls=(0, (3, 3)))
            a.annotate("", (0, y), (P, y), arrowprops=dict(arrowstyle="<->", color=INK, lw=1.2))
            a.text(P / 2, y * 1.04, f"one pitch period = {P:.1f} ms", ha="center", va="bottom",
                   fontsize=9.5)
            a.set_ylim(top=y * 1.35)
        if k == 0:
            a.set_ylabel("air pressure")
    save(fig, out, "waveform.svg")
    return iv, iu


def fig_harmonics(out, x, m, i):
    c = centre(i)
    fr = x[c - 160:c + 160]
    F = np.fft.rfft(fr * np.hanning(320), 4096)
    f = np.fft.rfftfreq(4096, 1 / FS)
    db = 20 * np.log10(np.abs(F) + 1e-9)
    L, wo = m[i]["L"], m[i]["Wo"]
    fh = np.arange(1, L + 1) * wo * FS / (2 * np.pi)
    ah = 20 * np.log10(m[i]["A"][1:L + 1])
    db += np.median(ah - np.interp(fh, f, db))          # put both on the same dB scale
    f0 = fh[0]
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(f, db, color=GRID, lw=1.2, zorder=1)
    ax.plot(f, db, color=INK2, lw=0.8, zorder=1, label="spectrum of 40 ms of the vowel")
    ax.vlines(fh, ah.min() - 20, ah, color=BLUE, lw=1.5, zorder=2)
    ax.plot(fh, ah, "o", ms=6, color=BLUE, mec=BG, mew=1.5, zorder=3,
            label=f"harmonics: whole-number multiples of {f0:.0f} Hz")
    ax.set_xlim(0, 4000)
    ax.set_ylim(ah.min() - 20, ah.max() + 12)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("loudness (dB)")
    ax.set_title(f"One vowel is a stack of harmonics, each {f0:.0f} Hz apart")
    for k in (1, 2, 3):
        ax.annotate(f"{k}×", (fh[k - 1], ah[k - 1]), xytext=(0, 9), textcoords="offset points",
                    ha="center", fontsize=9, color=INK2)
    ax.legend(loc="upper right")
    save(fig, out, "harmonics.svg")


def fig_pitch(out, xm, mm, xf, mf, mf_c2):
    """mm, mf: sotto's analysis (joint pitch and voicing tracker); mf_c2: Codec 2's own track."""
    t0, t1 = 0.0, 4.0
    fig, axs = plt.subplots(4, 1, figsize=(9, 9.4), sharex=True,
                            gridspec_kw=dict(height_ratios=[1.2, 1.2, 1, 1], hspace=0.36))
    for ax, x, title in ((axs[0], xm, "Man (ve9qrp)"), (axs[1], xf, "Woman (LibriSpeech 198)")):
        spectrogram(ax, x, t0, t1)
        ax.set_title(f"{title}: spectrogram. The thin stripes are harmonics.")
        ax.set_ylabel("frequency (Hz)")

    def track(ax, m, col, lab, **kw):
        t = np.array([centre(i) for i in range(len(m))]) / FS
        e = vl.frame_energy_db(m)
        f0 = np.where((m["voiced"] > 0) & (e > np.percentile(e, 95) - 30), m["Wo"] * FS / (2 * np.pi), np.nan)
        sel = (t >= t0) & (t < t1)
        ax.plot(t[sel], f0[sel], color=col, label=lab, **kw)
    ax = axs[2]
    track(ax, mm, BLUE, "man", lw=2)
    track(ax, mf, ORANGE, "woman", lw=2)
    ax.set_title("Pitch tracks from sotto's tracker (gaps = unvoiced or silent)")
    ax = axs[3]
    track(ax, mf_c2, INK2, "Codec 2's tracker", lw=1.2)
    track(ax, mf, ORANGE, "sotto's tracker", lw=2)
    ax.set_title("The woman's pitch: sotto's tracker removes many of Codec 2's octave jumps")
    ax.set_xlabel("time (seconds)")
    for ax in axs[2:]:
        ax.set_ylim(50, 500)
        ax.set_ylabel("pitch (Hz)")
        ax.legend(loc="upper right", ncol=2, fontsize=9)
    save(fig, out, "pitch.svg")


def fig_envelope(out, x, m, i):
    L, wo = m[i]["L"], m[i]["Wo"]
    fh = np.arange(1, L + 1) * wo * FS / (2 * np.pi)
    ah = 20 * np.log10(m[i]["A"][1:L + 1])
    f = np.linspace(50, 3900, 600)
    c = centre(i)
    lpc = lpc_envelope(x[c - 160:c + 160], 10, f)
    lpc += np.mean(ah - np.interp(fh, f, lpc))
    rk = vl.to_rate_k(m)[i]
    fig, axs = plt.subplots(1, 2, figsize=(9.5, 3.9), sharey=True, gridspec_kw=dict(wspace=0.08))
    for ax in axs:
        ax.plot(fh, ah, "o", ms=5, color=INK2, mec=BG, mew=1, label="harmonics (what we want to send)")
        ax.set_xlim(0, 4000)
        ax.set_xlabel("frequency (Hz)")
    axs[0].plot(f, lpc, color=VIOLET, label="LPC-10 curve")
    axs[0].set_title("Codec 2: a 10-number LPC curve")
    axs[0].set_ylabel("loudness (dB)")
    axs[1].plot(vl.KFREQ, rk, color=AQUA, lw=1.5, zorder=2)
    axs[1].plot(vl.KFREQ, rk, "s", ms=6.5, color=AQUA, mec=BG, mew=1.5, zorder=3,
                label="20 mel-spaced points")
    axs[1].set_title("sotto: 20 points, closer together at low pitch")
    for ax in axs:
        ax.legend(loc="lower left", fontsize=9)
    lo = min(ah.min(), rk.min()) - 22
    axs[0].set_ylim(lo, ah.max() + 8)
    save(fig, out, "envelope.svg")


def fig_dct(out, m, i):
    rk = vl.to_rate_k(m)[i]
    shape = rk - rk.mean()
    c = vl.DCT @ shape
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(vl.KFREQ, shape, "s", ms=6, color=INK2, mec=BG, mew=1, label="the 20 points (average removed)", zorder=3)
    for n, col in ((2, YELLOW), (5, ORANGE), (20, AQUA)):
        cc = np.zeros(vl.K)
        cc[:n] = c[:n]
        ax.plot(vl.KFREQ, vl.DCT.T @ cc, color=col, label=f"rebuilt from the first {n} waves"
                if n < 20 else "rebuilt from all 20 (exact)", lw=1.8)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("dB above or below average")
    ax.set_title("Adding up cosine waves rebuilds the shape")
    ax.legend(fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=2)
    ax.set_xlim(0, 4000)
    save(fig, out, "dct.svg")


def fig_vq(out, m, i, vq, sq):
    """The 3-stage shape VQ on one frame, and its error against v0-v2's scalar quantizer."""
    rk = vl.to_rate_k(m)
    shape = rk[i] - rk[i].mean()
    idx = vq.indices(shape)[0]
    fig, axs = plt.subplots(1, 2, figsize=(9.5, 3.9), gridspec_kw=dict(width_ratios=[1.6, 1], wspace=0.3))
    ax = axs[0]
    ax.plot(vl.KFREQ, shape, "s", ms=6, color=INK2, mec=BG, mew=1, label="the shape to send", zorder=3)
    c = np.zeros(vl.K)
    for s_, (col, lab) in enumerate(((YELLOW, "stage 1 (12 bits)"), (ORANGE, "+ stage 2 (11 bits)"),
                                     (AQUA, "+ stage 3 (11 bits)"))):
        c[1:] += vq.cb[s_][idx[s_]]
        ax.plot(vl.KFREQ, vl.DCT.T @ c, color=col, lw=1.8, label=lab)
    ax.set_xlim(0, 4000)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("dB above or below average")
    ax.set_title("Three codebook entries, added up")
    ax.legend(fontsize=8.5, loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=2)
    # mean squared error over the clip's speech frames
    e = vl.frame_energy_db(m)
    keep = e > np.percentile(e, 95) - 30
    sh = rk[keep] - rk[keep].mean(1, keepdims=True)
    err = []
    for k in (1, 2, 3):
        cs_ = np.zeros((len(sh), vl.K))
        ii = vq.indices(sh)
        for s_ in range(k):
            cs_[:, 1:] += vq.cb[s_][ii[:, s_]]
        err.append(np.mean((sh - cs_ @ vl.DCT) ** 2))
    err_sq = np.mean((sh - sq.quantize(sh)) ** 2)
    ax = axs[1]
    labels = ["VQ\n1 stage\n12 bits", "VQ\n2 stages\n23 bits", "VQ\n3 stages\n34 bits", "scalar\n(v0–v2)\n34 bits"]
    vals = err + [err_sq]
    ax.bar(range(4), vals, color=[AQUA, AQUA, AQUA, INK2], width=0.65)
    for k, v in enumerate(vals):
        ax.text(k, v + 0.2, f"{v:.1f}", ha="center", va="bottom", fontsize=9, color=INK2)
    ax.set_xticks(range(4))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("average error (dB²)")
    ax.set_title("Error on this clip")
    ax.grid(axis="x", visible=False)
    ax.set_ylim(0, max(vals) * 1.2)
    save(fig, out, "vq.svg")
    return err, err_sq


def fig_mixed(out, m, i):
    """Spectrum of the same vowel synthesised fully voiced and as mixed excitation."""
    seg = m[i - 30:i + 30].copy()
    vo, mx = seg.copy(), seg.copy()
    vo["voiced"] = 1
    mx["voiced"] = [vl.mixed_cutoff(w) for w in mx["Wo"]]
    fig, axs = plt.subplots(1, 2, figsize=(9.5, 3.7), sharey=True, gridspec_kw=dict(wspace=0.08))
    for ax, mm_, title, col in ((axs[0], vo, "Voiced: harmonics all the way up", BLUE),
                                (axs[1], mx, "Mixed: harmonics below 1 kHz, noise above", VIOLET)):
        y = vl.synthesise(mm_)[400:-400]
        f, P = signal.welch(y, FS, nperseg=512)
        ax.plot(f, 10 * np.log10(P + 1e-12), color=col, lw=1.2)
        ax.axvline(1000, color=INK2, lw=0.8, ls=(0, (3, 3)))
        ax.set_title(title, fontsize=11)
        ax.set_xlim(0, 4000)
        ax.set_xlabel("frequency (Hz)")
    axs[0].set_ylabel("loudness (dB)")
    lo = axs[0].get_ylim()
    axs[0].set_ylim(max(lo[0], lo[1] - 80), lo[1])
    save(fig, out, "mixed.svg")


def fig_conceal(out):
    """Output level during a run of lost frames, from the gain rule in SottoV34.decode()
    (10 ms halves of 20 ms frames), and the older receiver rule (repeat 3 frames, then mute)."""
    t = np.arange(0, 400, 10)                           # ms since the first lost frame

    def level(hold, slope, max_lost):
        j = t // 20 + 1                                 # which lost frame (1 = first)
        half = (t % 20) // 10
        g = -slope * np.maximum(0, j - hold + half / 2)
        return np.where(j > max_lost, -80.0, g)
    old = np.where(t // 20 + 1 > 3, -80.0, 0.0)
    fig, ax = plt.subplots(figsize=(9, 3.8))
    for y, col, lab in ((old, INK2, "receiver repeats 3 frames, then mutes (Codec 2; sotto to v3.3)"),
                        (level(1, 2.0, 15), YELLOW, "v3.4: slow fade over 300 ms"),
                        (level(2, 6.0, 4), BLUE, "v3.5: hold 40 ms, fade 6 dB per 20 ms, silent after 80 ms")):
        ax.step(np.r_[-20, t], np.maximum(np.r_[0, y], -40), where="post", color=col, lw=2, label=lab)
    ax.set_ylim(-42, 3)
    ax.set_xlim(-20, 400)
    ax.set_yticks([0, -10, -20, -30, -40])
    ax.set_yticklabels(["0", "−10", "−20", "−30", "silent"])
    ax.set_xlabel("time since the first lost frame (ms)")
    ax.set_ylabel("output level (dB)")
    ax.set_title("What the decoder plays while frames are lost")
    ax.legend(loc="upper right", fontsize=9)
    save(fig, out, "conceal.svg")


def fig_quantize(out, m):
    e = vl.frame_energy_db(m)
    lo, hi = vl.SottoV0.E_LO, vl.SottoV0.E_HI
    _, eq = vl.q_uniform(e, lo, hi, 5)
    t = np.array([centre(i) for i in range(len(m))]) / FS
    sel = (t >= 1.0) & (t < 2.0)
    fig, axs = plt.subplots(1, 2, figsize=(9.5, 3.8), gridspec_kw=dict(width_ratios=[1, 1.5], wspace=0.25))
    ax = axs[0]
    xs = np.linspace(lo - 3, hi + 3, 800)
    _, ys = vl.q_uniform(xs, lo, hi, 5)
    ax.plot(xs, xs, color=GRID, lw=1.2)
    ax.plot(xs, ys, color=BLUE, lw=1.6, drawstyle="steps-mid")
    ax.set_xlabel("true loudness (dB)")
    ax.set_ylabel("loudness the receiver gets (dB)")
    ax.set_title("5 bits = 32 steps")
    ax.text(lo + 2, hi - 6, f"each step is\n{(hi - lo) / 31:.2f} dB", fontsize=9.5, color=INK2, va="top")
    ax = axs[1]
    ax.plot(t[sel], e[sel], color=INK2, lw=1.2, label="true frame energy")
    ax.plot(t[sel], eq[sel], color=BLUE, lw=1.8, drawstyle="steps-mid", label="after 5-bit quantizing")
    ax.set_xlabel("time (seconds)")
    ax.set_ylabel("loudness (dB)")
    ax.set_title("Frame energy, one second of speech")
    ax.legend(loc="lower right", fontsize=9)
    save(fig, out, "quantize.svg")


def fig_synthesis(out, x, m, i):
    c = centre(i)
    seg = x[c - 80:c + 80]                         # 20 ms
    L = m[i]["L"]

    def resid(w):
        a, p, n = harmonic_fit(seg, w, L)
        return np.sum((seg - sum(a[k] * np.cos((k + 1) * w * n + p[k]) for k in range(L))) ** 2)
    wo = min(m[i]["Wo"] * np.linspace(0.97, 1.03, 61), key=resid)   # fine-tune the pitch
    amp, ph, n = harmonic_fit(seg, wo, L)
    tt = n / FS * 1000

    def wave(ks):
        return sum(amp[k - 1] * np.cos(k * wo * n + ph[k - 1]) for k in ks)
    fig, axs = plt.subplots(2, 1, figsize=(9, 6), gridspec_kw=dict(hspace=0.42))
    ax = axs[0]
    sc = np.max(np.abs(seg))
    for j, k in enumerate((1, 2, 3, 4)):
        ax.plot(tt, wave([k]) / sc - 1.0 * j, color=[BLUE, ORANGE, AQUA, VIOLET][j], lw=1.6)
        ax.text(tt[-1] + 0.3, -1.0 * j, f"harmonic {k}\n{k * wo * FS / 2 / np.pi:.0f} Hz",
                va="center", fontsize=9, color=INK2)
    ax.set_yticks([])
    ax.set_xlim(tt[0], tt[-1])
    ax.set_title("Step 1: make one sine wave per harmonic (only the first 4 shown)")
    ax.set_xlabel("time (milliseconds)")
    ax = axs[1]
    ax.plot(tt, seg, color=GRID, lw=5, label="original vowel")
    ax.plot(tt, wave(range(1, 4)), color=YELLOW, lw=1.6, label="sum of the first 3")
    ax.plot(tt, wave(range(1, L + 1)), color=BLUE, lw=1.6, label=f"sum of all {L}")
    ax.set_yticks([0])
    ax.set_xlim(tt[0], tt[-1])
    ax.set_title("Step 2: add them up. With all the harmonics we get the vowel back.")
    ax.set_xlabel("time (milliseconds)")
    ax.legend(loc="upper right", ncol=3, fontsize=9)
    ax.set_ylim(seg.min() * 1.15, seg.max() * 1.6)
    save(fig, out, "synthesis.svg")


def fig_rebuild(out, voice):
    x = load(voice + "__original")
    files = [("Original speech", x),
             ("Codec 2 2400, no radio", load(f"{voice}__No_radio_channel__codec2_2400", "encoder")),
             ("sotto v3.5, no radio", load(f"{voice}__No_radio_channel__sotto_v35", "encoder")),
             ("sotto v3.5 over a weak 446 MHz mobile signal",
              load(f"{voice}__446_MHz_60_kmh_52_dB-Hz__sotto_v35", "encoder"))]
    t0, t1 = 1.0, 3.5
    fig, axs = plt.subplots(len(files), 1, figsize=(9, 9), sharex=True, gridspec_kw=dict(hspace=0.32))
    top = None
    for ax, (title, y) in zip(axs, files):
        lag = best_lag(x, y)
        top = spectrogram(ax, y[lag:], t0, t1, top)
        ax.set_title(title)
        ax.set_ylabel("Hz")
    axs[-1].set_xlabel("time (seconds)")
    axs[-1].set_xticks(np.arange(0, t1 - t0 + 0.01, 0.5))
    axs[-1].set_xticklabels([f"{t0 + v:.1f}" for v in np.arange(0, t1 - t0 + 0.01, 0.5)])
    save(fig, out, "rebuild.svg")


def best_lag(r, y, max_lag=400):
    n = min(len(r), len(y)) - max_lag
    c = [np.dot(r[:n], y[k:k + n]) for k in range(max_lag)]
    return int(np.argmax(c))


def fig_ladder(out):
    """The quantization (oracle) ladder for Codec 2 from docs/results/oracle_ladder_15s.json,
    with sotto v3.5 from docs/results/encoder_race.json as the last rung (scored with the later
    fixed 160-sample alignment, so it is only roughly comparable)."""
    import json
    res = os.path.join(ROOT, "docs", "results")
    lad = json.load(open(os.path.join(res, "oracle_ladder_15s.json")))
    race = json.load(open(os.path.join(res, "encoder_race.json")))
    labels = ["Harmonic model,\ntrue phases", "+ made-up\nphases", "+ postfilter",
              "+ LPC-10\nenvelope", "+ quantized\nLSPs", "+ quantized pitch\nand energy, 20 ms",
              "Codec 2 2400\n(real codec)"]
    voices = ((MALE, BLUE, "man (ve9qrp)"), ("male_libri3436_15s", AQUA, "man (LibriSpeech)"),
              (FEMALE, ORANGE, "woman (LibriSpeech)"))
    fig, ax = plt.subplots(figsize=(9.5, 4.6))
    x = np.arange(len(labels))
    for v, col, lab in voices:
        y = [p for p, _ in lad[v].values()]
        ax.plot(x, y, "-o", color=col, ms=7, mec=BG, mew=1.5, label=lab)
        s0 = next(r["per_clip"][v]["pesq"] for r in race
                  if r["codec"] == "sotto_v35" and r["cond"] == "No radio channel")
        ax.plot(len(labels), s0, "D", ms=8, color=col, mec=BG, mew=1.5)
        ax.plot([len(labels) - 1, len(labels)], [y[-1], s0], color=col, lw=1.2, ls=(0, (3, 3)))
    ax.set_xticks(list(x) + [len(labels)])
    ax.set_xticklabels(labels + ["sotto v3.5\n(48 bits)"], fontsize=8.5)
    ax.axvspan(2.6, 3.4, color=VIOLET, alpha=0.1, lw=0)
    ax.text(3, 4.05, "biggest step\ndown for men", ha="center", va="top", fontsize=9, color=VIOLET)
    ax.set_ylabel("PESQ score (higher = better)")
    ax.set_ylim(1.9, 4.1)
    ax.set_title("The quantization ladder for Codec 2: one simplification per rung")
    ax.legend(loc="lower left", fontsize=9)
    ax.grid(axis="x", visible=False)
    save(fig, out, "ladder.svg")


# PESQ per step, from the table "Where the 48-bit codec loses quality" in docs/encoder-v1.md
V1_LADDER = [("Harmonic model,\nsotto tracker", (3.10, 3.60, 3.56)),
             ("+ 20-point\nenvelope", (2.90, 3.46, 3.17)),
             ("+ envelope sent\nevery 20 ms", (2.76, 3.35, 2.96)),
             ("+ pitch sent\nevery 20 ms", (2.65, 3.25, 2.96)),
             ("+ 7-bit pitch,\n34-bit shape", (2.45, 3.00, 2.55)),
             ("+ formant\npostfilter (= v1)", (2.62, 3.24, 2.69))]


def fig_ladder_v1(out):
    voices = ((BLUE, "man (ve9qrp)", 2), (AQUA, "man (LibriSpeech)", 1), (ORANGE, "woman (LibriSpeech)", 0))
    fig, ax = plt.subplots(figsize=(9.5, 4.3))
    x = np.arange(len(V1_LADDER))
    for col, lab, k in voices:
        ax.plot(x, [v[k] for _, v in V1_LADDER], "-o", color=col, ms=7, mec=BG, mew=1.5, label=lab)
    ax.set_xticks(x)
    ax.set_xticklabels([l for l, _ in V1_LADDER], fontsize=8.5)
    ax.set_ylabel("PESQ score (higher = better)")
    ax.set_ylim(2.3, 3.75)
    ax.set_title("The quantization ladder for sotto v1")
    ax.legend(loc="lower left", fontsize=9)
    ax.grid(axis="x", visible=False)
    save(fig, out, "ladder-v1.svg")


def fig_c4fm(out):
    rng = np.random.default_rng(7)
    frames = Transmitter().frames(rng.integers(0, 2, (5, 48)))
    bits = frames[1]
    f = c4fm.Modulator().modulate(bits, lead_s=0, tail_s=0)
    nsym = 24
    t = np.arange(nsym * c4fm.SPS) / c4fm.FS * 1000
    lv = c4fm.dibits_to_levels(bits[:2 * nsym]) * c4fm.DEV_HZ
    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.plot(t, f[:nsym * c4fm.SPS], color=BLUE, lw=1.6)
    ts = np.arange(nsym) * c4fm.SPS / c4fm.FS * 1000
    ax.plot(ts, lv, "o", ms=6, color=ORANGE, mec=BG, mew=1.5, zorder=3)
    for k in range(nsym):
        ax.text(ts[k], lv[k] + (230 if lv[k] > 0 else -230), f"{bits[2 * k]}{bits[2 * k + 1]}",
                ha="center", va="center", fontsize=8, color=INK2)
    ax.set_yticks([-1800, -600, 600, 1800])
    ax.set_yticklabels(["−1800 Hz  (11)", "−600 Hz  (10)", "+600 Hz  (00)", "+1800 Hz  (01)"])
    ax.set_ylim(-2400, 2400)
    ax.set_xlabel("time (milliseconds)")
    ax.set_title("C4FM: every 0.21 ms the radio jumps to one of 4 frequencies = 2 bits")
    save(fig, out, "c4fm.svg")


def fig_fading(out):
    fig, axs = plt.subplots(2, 1, figsize=(9, 4.8), sharex=True, sharey=True, gridspec_kw=dict(hspace=0.4))
    n = 2 * cs.RS
    t = np.arange(n) / cs.RS
    for ax, (mhz, kmh), seed in zip(axs, ((446, 60), (146, 30)), (3, 5)):
        fd = cs.doppler(mhz * 1e6, kmh)
        h = cs.rayleigh(n, fd, np.random.default_rng(seed))
        db = 20 * np.log10(np.abs(h) + 1e-6)
        ax.plot(t, db, color=BLUE, lw=1.2)
        ax.axhspan(-40, -10, color=ORANGE, alpha=0.12, lw=0)
        ax.set_title(f"{mhz} MHz at {kmh} km/h: about {fd:.0f} fades per second")
        ax.set_ylabel("signal (dB)")
    axs[0].set_ylim(-35, 10)
    axs[0].text(0.005, -33, "deep fades: bits get lost", ha="left", va="bottom", fontsize=9, color=ORANGE,
                transform=axs[0].get_yaxis_transform(), zorder=0)
    axs[1].set_xlabel("time (seconds)")
    save(fig, out, "fading.svg")


def fig_spread(out):
    """Which of the five vocoder frames owns each of the 720 bits on the air, with and without
    spreading, and what a 10 ms fade does to each."""
    rng = np.random.default_rng(11)
    vw = payload.SottoVW()
    frames = rng.integers(0, 2, (5, 48)).astype(np.int8)
    u = payload._add_crc(frames)
    coded = vw.CODE.encode(u)
    # owner of each coded bit: the frame whose info bit drove that output
    per_in = np.repeat(np.arange(5), 56)
    keep = vw.CODE._mask(len(u))
    owner = np.repeat(per_in, 3).reshape(-1, 3)[keep]
    air_plain = owner
    air_spread = np.zeros(720, int)
    air_spread[vw.PERM] = owner
    burst = slice(330, 330 + 96)                   # 96 bits = 48 symbols = 10 ms

    def survive(spread):
        llr = np.where(coded == 0, 6.0, -6.0)
        if spread:
            a = np.zeros(720)
            a[vw.PERM] = llr
            a[burst] = rng.normal(0, 2, 96)
            llr = a[vw.PERM]
        else:
            llr = llr.copy()
            llr[burst] = rng.normal(0, 2, 96)
        dec = vw.CODE.decode(llr, len(u))
        return payload._check_crc(dec)[1]
    ok_plain, ok_spread = survive(False), survive(True)
    cols = np.array([BLUE, ORANGE, AQUA, YELLOW, VIOLET])
    fig, axs = plt.subplots(2, 1, figsize=(9, 3.6), sharex=True, gridspec_kw=dict(hspace=0.9))
    for ax, air, ok, title in ((axs[0], air_plain, ok_plain, "Without spreading"),
                               (axs[1], air_spread, ok_spread, "With sotto's spreading")):
        rgb = np.array([[int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)] for c in cols])
        ax.imshow(rgb[air][None], aspect="auto", extent=(0, 720, 0, 1), interpolation="nearest")
        ax.axvspan(burst.start, burst.stop, color=INK, alpha=0.35, lw=0)
        ax.set_yticks([])
        ax.set_xlim(0, 720)
        ax.grid(False)
        lost = [str(k + 1) for k in range(5) if not ok[k]]
        verdict = f"frames lost: {', '.join(lost)}" if lost else "all 5 frames decoded correctly"
        ax.set_title(f"{title}: {verdict}", fontsize=11)
    for ax in axs:
        ax.text((burst.start + burst.stop) / 2, 0.5, "10 ms\nfade", ha="center", va="center",
                fontsize=9, color=BG, fontweight="bold")
    axs[1].set_xlabel("bit position in the 100 ms frame's payload (0 to 719)")
    hs = [plt.Rectangle((0, 0), 1, 1, color=c) for c in cols]
    fig.legend(hs, [f"frame {k}" for k in range(1, 6)], loc="lower center", ncol=5,
               bbox_to_anchor=(0.5, -0.1), fontsize=9)
    save(fig, out, "spread.svg")
    return ok_plain, ok_spread


# ---------------------------------------------------------------- listening demos
def demos(out, x):
    """Short clips that each break one part of the model, so you can hear what it does."""
    x = x[:int(6 * FS)]
    d = os.path.join(out, "audio")
    os.makedirs(d, exist_ok=True)
    m = vl.analyse(x, PITCH)
    codec = CODEC()
    robot = vl.set_wo(m, np.full(len(m), 2 * np.pi * 110 / FS), m["voiced"])
    robot = vl.from_rate_k(robot, vl.to_rate_k(m))
    whisper = m.copy()
    whisper["voiced"] = 0
    mixed = m.copy()
    mixed["voiced"] = [vl.mixed_cutoff(w) if v else 0 for w, v in zip(m["Wo"], m["voiced"])]
    flat = m.copy()
    for r in flat:
        L = r["L"]
        r["A"][1:L + 1] = np.sqrt(np.sum(r["A"][1:L + 1] ** 2) / L)
    clips = {
        "original": x,
        "model": vl.synthesise(m),
        "whisper": vl.synthesise(whisper),
        "monotone": vl.synthesise(robot),
        "flat_envelope": vl.synthesise(flat, postfilter=False),
        "mixed": vl.synthesise(mixed),
        "sotto_v35": codec.decode_audio(codec.encode(x)),
    }
    for name, y in clips.items():
        y = np.asarray(y)[:len(x)]
        y = 0.6 * y * np.sqrt(np.mean(x ** 2) / (np.mean(y ** 2) + 1e-12))   # same loudness, headroom
        write_flac(os.path.join(d, f"{name}.flac"), np.clip(y, -1, 1), FS)
        print("wrote audio/", name)

# ---------------------------------------------------------------- chapter 8: the hiss band
HISS_VOICE, HISS_T0, HISS_T1 = "librispeech_8842_f", 7.8, 9.6   # a phrase with a strong hiss at 8.7 s


def sotto_v38_c(x16):
    """Encode and decode 16 kHz speech with the C port's v3.8 tools (c/sotto_enc -16,
    c/sotto_dec -16, default VARIANT=v3.8 build). The output is aligned with the input."""
    enc, dec = os.path.join(ROOT, "c", "sotto_enc"), os.path.join(ROOT, "c", "sotto_dec")
    with tempfile.TemporaryDirectory() as d:
        raw, bit, outp = (os.path.join(d, n) for n in ("in.raw", "x.bit", "out.raw"))
        (np.clip(x16, -1, 1) * 32767).astype("<i2").tofile(raw)
        subprocess.run([enc, "-16", raw, bit], check=True)
        subprocess.run([dec, "-16", bit, outp], check=True)
        return np.fromfile(outp, "<i2") / 32768.0


def fig_hiss(out):
    """The same phrase as the 16 kHz original, sotto v3.7 (8 kHz, Python reference) and sotto
    v3.8 (16 kHz, C port), drawn 0-8 kHz; and the three as short 16 kHz demo clips."""
    x16, fs = sf.read(os.path.join(SAMPLES, "v38", HISS_VOICE + "__original16.flac"))
    assert fs == 16000
    x8 = resample_poly(x16, 1, 2)
    y37 = np.asarray(vl.SottoV37().decode_audio(vl.SottoV37().encode(x8)))
    y37 = y37[best_lag(x8, y37):]
    y38 = sotto_v38_c(x16)
    rows = [("Original speech, 16,000 samples per second", x16, 16000),
            ("sotto v3.7: 8,000 samples per second, nothing above 4,000 Hz", y37, 8000),
            ("sotto v3.8: still 48 bits per frame, plus the 4,000-8,000 Hz hiss band", y38, 16000)]
    t0, t1 = HISS_T0, HISS_T1
    fig, axs = plt.subplots(3, 1, figsize=(9, 8.2), sharex=True, gridspec_kw=dict(hspace=0.34))
    top = None
    for ax, (title, y, r) in zip(axs, rows):
        f, t, S = signal.spectrogram(y[int(t0 * r):int(t1 * r)], r, window="hann",
                                     nperseg=r // 31, noverlap=r // 31 * 7 // 8, nfft=1024)
        S = 10 * np.log10(S + 1e-12)
        top = S.max() if top is None else top
        ax.pcolormesh(t, f, S, cmap=SPEC, vmin=top - 70, vmax=top, shading="auto", rasterized=True)
        ax.axhline(4000, color=ORANGE, lw=1.2, ls="--")
        ax.set_ylim(0, 8000)
        ax.set_xlim(0, t1 - t0)
        ax.grid(False)
        ax.set_title(title)
        ax.set_ylabel("Hz")
    axs[0].annotate("the hiss of a fricative", xy=(0.93, 5600), xytext=(1.2, 7000), color=INK,
                    arrowprops=dict(arrowstyle="->", color=INK))
    axs[1].text(0.02, 6000, "nothing is sent up here", color=INK2)
    axs[-1].set_xlabel("time (seconds)")
    ticks = np.arange(0, t1 - t0 + 0.01, 0.4)
    axs[-1].set_xticks(ticks)
    axs[-1].set_xticklabels([f"{t0 + v:.1f}" for v in ticks])
    save(fig, out, "hiss.svg")

    d = os.path.join(out, "audio")
    os.makedirs(d, exist_ok=True)
    y37_16 = resample_poly(y37, 2, 1)
    for name, y in (("hiss_original", x16), ("hiss_v37", y37_16), ("hiss_v38", y38)):
        y = np.asarray(y)[int(t0 * 16000):int(t1 * 16000)]
        y = 0.6 * y / (np.abs(x16[int(t0 * 16000):int(t1 * 16000)]).max() + 1e-12)
        write_flac(os.path.join(d, f"{name}.flac"), np.clip(y, -1, 1), 16000)
        print("wrote audio/", name)


def main(out):
    os.makedirs(os.path.join(out, "figures"), exist_ok=True)
    xm, xf = load(MALE + "__original"), load(FEMALE + "__original")
    mm, mf = vl.analyse(xm, PITCH), vl.analyse(xf, PITCH)      # sotto's analysis
    iv, _ = fig_waveform(out, xm, mm)
    fig_harmonics(out, xm, mm, iv)
    fig_pitch(out, xm, mm, xf, mf, vl.analyse(xf))
    fig_envelope(out, xm, mm, iv)
    fig_dct(out, mm, iv)
    sq = vl.ShapeQ(vl.training_set("tracked"))
    print("VQ error by stage, scalar error:", *fig_vq(out, mm, iv, vl.ShapeMSVQ.load(), sq))
    fig_quantize(out, mm)
    fig_synthesis(out, xm, mm, iv)
    fig_mixed(out, mm, iv)
    fig_rebuild(out, FEMALE)
    fig_ladder(out)
    fig_ladder_v1(out)
    fig_c4fm(out)
    fig_fading(out)
    fig_conceal(out)
    print("spread demo (plain, spread):", *fig_spread(out))
    demos(out, xm)
    fig_hiss(out)


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--out", default=os.path.join(ROOT, "docs", "course"))
    a.add_argument("--hiss-only", action="store_true", help="only the chapter 8 figure and clips")
    args = a.parse_args()
    if args.hiss_only:
        os.makedirs(os.path.join(args.out, "figures"), exist_ok=True)
        fig_hiss(args.out)
    else:
        main(args.out)
