"""Bitstreams and scores for the v3.8 rows of the listening page (docs/index.html).

The page decodes sotto v3.8 in the browser (c/wasm, docs/sotto.wasm), so it stores what a
receiver hands the decoder instead of audio: the received 6-byte frames (`.bit`) and, after a
radio channel, one byte per frame that is 0 where the frame failed its CRC (`.good`). The same
files decode with the C tool: `c/sotto_dec -16 X.bit out.raw X.good`.

The chain is sim/encoder_race.py's (sotto VW payload, C4FM softmodem, FM channel with the
same fading seeds, the softmodem receiver), with the C v3.8 encoder (c/sotto_enc -16) at the
front and the C v3.8 decoder at the end. PESQ is narrowband (8 kHz, like every other score on
the page), so it hears only the 0-4 kHz core, not the 4-8 kHz hiss band; mean of the three
fading seeds, the page plays the first. With --codec2, Codec 2 2400 goes through the same
chain from the same 8 kHz input (x16 downsampled), scored against the same reference, and the
first seed is written as <voice>__<condition>__codec2_2400.flac for the page's Codec 2 rows.

Usage: python tools/render_page_v38.py --libri DIR [--dev-clean DIR] [--out docs/samples/v38] [--codec2]
  --libri: 198-209-0000.ogg and 3436-172162-0000.ogg from librosa's example data
  (github.com/librosa/data, audio/), the 16 kHz sources of the page's three fading voices.
  --dev-clean: the LibriSpeech folder (SPEAKERS.TXT, dev-clean/) for the "more voices" part:
  the 8 speakers tools/clean_corpus.py holds out of codebook training, the same 12 s clips as
  the 8 kHz test set, at 16 kHz, no radio channel only.
  ve9qrp exists only at 8 kHz; it is upsampled, so it has no 4-8 kHz band to code (the decoder
  still predicts an upper band for its voiced frames).
Build first: make (c/sotto_enc, c/sotto_dec with VARIANT=v3.8, the default).
"""
import argparse, json, os, subprocess, sys, tempfile
import numpy as np, soundfile as sf
from scipy.signal import resample_poly

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, ROOT)
import audio_race as ar                                        # noqa: E402
import encoder_race as er                                      # noqa: E402
import waveforms as wf                                         # noqa: E402
from modem.link import Transmitter, Receiver, CODEC2_2400_MUTE  # noqa: E402

VOICES = [("female_libri198_14s", "198-209-0000.ogg", None),
          ("male_libri3436_15s", "3436-172162-0000.ogg", 15.0),
          ("male_ve9qrp_15s", None, None)]
CONDS = [c for c in er.CONDS if c["name"] != "446 MHz 60 km/h 56 dB-Hz"]   # the page's three
SEEDS = (1, 2, 3)
MUTE = np.zeros(48, np.int8)


def source16(libri, voice, ogg, seconds):
    """16 kHz float source, peak 0.5 like the page's 8 kHz originals."""
    if ogg is None:
        x8, fs = sf.read(os.path.join(ROOT, "docs", "samples", f"{voice}__original.flac"))
        x = resample_poly(x8, 2, 1)
    else:
        x, fs = sf.read(os.path.join(libri, ogg))
        if x.ndim > 1:
            x = x[:, 0]
        x = resample_poly(x, 320, 441) if fs == 22050 else resample_poly(x, 16000, fs)
        if seconds:
            x = x[:int(seconds * 16000)]
    x = x[:len(x) // 320 * 320]
    return 0.5 * x / np.max(np.abs(x))


HELD_OUT = ["2428", "251", "3081", "3536", "6313", "652", "777", "8842"]   # clean_corpus.py --test 8


def held_out16(d, seconds=12.0):
    """[(name, 16 kHz clip)]: clean_corpus.test_clip's choice of utterance, at 16 kHz."""
    import clean_corpus as cc
    spk = cc.librispeech(d)
    out = []
    for s in HELD_OUT:
        sex, files = spk[s]
        lens = [sf.info(f).duration for f in files]
        f = next((f for f, n in zip(files, lens) if n >= seconds), files[int(np.argmax(lens))])
        x, fs = sf.read(f)
        assert fs == 16000
        x = x[:int(seconds * 16000)]
        x = x[:len(x) // 320 * 320]
        out.append((f"librispeech_{s}_{sex.lower()}", 0.5 * x / np.max(np.abs(x))))
    return out


def c_encode(x16):
    with tempfile.TemporaryDirectory() as d:
        np.round(x16 * 32767).astype("<i2").tofile(f"{d}/in.raw")
        subprocess.run([f"{ROOT}/c/sotto_enc", "-16", f"{d}/in.raw", f"{d}/out.bit"], check=True)
        return np.unpackbits(np.fromfile(f"{d}/out.bit", np.uint8)).reshape(-1, 48).astype(np.int8)


def c_decode(bit_path, good_path=None):
    with tempfile.TemporaryDirectory() as d:
        args = [f"{ROOT}/c/sotto_dec", "-16", bit_path, f"{d}/out.raw"] + ([good_path] if good_path else [])
        subprocess.run(args, check=True)
        return np.fromfile(f"{d}/out.raw", "<i2").astype(float) / 32768


def main(libri, out, codec2=False, dev_clean=None):
    os.makedirs(out, exist_ok=True)
    rows = []
    items = []
    if libri:
        items += [(v, source16(libri, v, ogg, sec), CONDS, ogg is not None) for v, ogg, sec in VOICES]
    if dev_clean:
        items += [(v, x, CONDS[:1], True) for v, x in held_out16(dev_clean)]
    for voice, x16, conds, write16 in items:
        if write16:                          # ve9qrp's page original stays the 8 kHz file
            sf.write(os.path.join(out, f"{voice}__original16.flac"), x16, 16000, subtype="PCM_16")
        x8 = resample_poly(x16, 1, 2)
        bits = c_encode(x16)
        n = len(bits)
        for cond in conds:
            scores = []
            for sd in (SEEDS if cond["cn0"] else SEEDS[:1]):
                if cond["cn0"] is None:
                    fr, good = bits, None
                else:
                    tx = Transmitter("sotto_vw").modulate(bits)
                    # same seed rule as encoder_race (len of the clip name)
                    disc, _ = wf.fm_channel(tx, cond["cn0"], fd=cond["fd"], seed=sd * 100 + len(voice))
                    rx = Receiver("sotto_vw", mute=MUTE).receive(disc, t0=ar.LEAD + 4800)
                    fr = np.concatenate([rx["frames"], np.repeat(MUTE[None], max(0, n - len(rx["frames"])), 0)])[:n]
                    good = np.concatenate([rx["good"], np.zeros(max(0, n - len(rx["good"])), bool)])[:n]
                base = os.path.join(out, f"{voice}__{er.tag(cond['name'])}")
                with tempfile.TemporaryDirectory() as d:
                    bp, gp = (base + ".bit", base + ".good") if sd == SEEDS[0] else (f"{d}/x.bit", f"{d}/x.good")
                    np.packbits(np.asarray(fr, np.uint8).reshape(-1, 48), axis=1).tofile(bp)
                    if good is not None:
                        np.asarray(good, np.uint8).tofile(gp)
                    y16 = c_decode(bp, gp if good is not None else None)
                y8 = resample_poly(y16, 1, 2)
                p, e = ar.score(x8, y8, 0)          # sotto_dec -16 output is aligned with the input
                scores.append(dict(seed=sd, pesq=float(p), estoi=float(e),
                                   lost=int(n - np.sum(good)) if good is not None else 0))
            if codec2:
                c2 = []
                b2 = ar.c2enc(np.round(x8 * 32767))
                for sd in (SEEDS if cond["cn0"] else SEEDS[:1]):
                    fr2 = b2
                    if cond["cn0"] is not None:
                        tx = Transmitter("sotto_vw").modulate(b2)
                        disc, _ = wf.fm_channel(tx, cond["cn0"], fd=cond["fd"], seed=sd * 100 + len(voice))
                        rx = Receiver("sotto_vw", mute=CODEC2_2400_MUTE).receive(disc, t0=ar.LEAD + 4800)
                        m = len(b2)
                        fr2 = np.concatenate([rx["frames"], np.repeat(CODEC2_2400_MUTE[None], max(0, m - len(rx["frames"])), 0)])[:m]
                    y2 = ar.c2dec(fr2)
                    c2.append(float(ar.score(x8, y2, ar.CODEC_DELAY)[0]))
                    if sd == SEEDS[0]:
                        sf.write(f"{base}__codec2_2400.flac", y2, 8000, subtype="PCM_16")
                codec2_pesq = float(np.mean(c2))
            r = dict(voice=voice, cond=cond["name"], frames=n, runs=scores,
                     pesq=float(np.mean([s["pesq"] for s in scores])),
                     estoi=float(np.mean([s["estoi"] for s in scores])))
            if codec2:
                r["codec2_pesq"] = codec2_pesq
            print(json.dumps(dict(voice=voice, cond=cond["name"], pesq=round(r["pesq"], 3),
                                  codec2=round(r.get("codec2_pesq", 0), 3),
                                  estoi=round(r["estoi"], 3), lost=[s["lost"] for s in scores])), flush=True)
            rows.append(r)
    return rows


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--libri")
    a.add_argument("--dev-clean")
    a.add_argument("--out", default=os.path.join(ROOT, "docs", "samples", "v38"))
    a.add_argument("--codec2", action="store_true")
    a.add_argument("--json", default=os.path.join(ROOT, "docs", "results", "page_v38.json"))
    a = a.parse_args()
    rows = main(a.libri, a.out, a.codec2, a.dev_clean)
    if os.path.exists(a.json):                 # keep the rows of voices not rendered this time
        done = {(r["voice"], r["cond"]) for r in rows}
        rows = [r for r in json.load(open(a.json)) if (r["voice"], r["cond"]) not in done] + rows
    with open(a.json, "w") as f:
        json.dump(rows, f, indent=1)
