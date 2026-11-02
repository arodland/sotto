# Listening samples

These are 8 kHz, 16-bit FLAC files, written with [HamFLAC](https://github.com/baitisj/hamflac). They were made by `sim/audio_race.py --render` (see `docs/audio-race.md`). Each clip is Codec 2 2400 sent through the C4FM softmodem, a simulated FM channel and the real softmodem receiver.

For each of the three voices there are:

| File suffix | What it is |
|---|---|
| `__original` | the input speech |
| `__codec2_2400_no_channel` | Codec 2 2400 with no radio channel (the best any scheme can do) |
| `__446_MHz_60_kmh_52_dB-Hz__sotto_vw` | sotto in the VW slot, UHF mobile, a fairly weak signal |
| `__446_MHz_60_kmh_52_dB-Hz__dn_conv` | Codec 2 in the DN slot with our convolutional code, same channel |
| `__446_MHz_60_kmh_52_dB-Hz__dn_rep` | Codec 2 in the DN slot with Fusion-style protection (27 bits ×3), same channel |
| `__146_MHz_30_kmh_56_dB-Hz__sotto_vw` | sotto in the VW slot, slow VHF fading (the hard case) |

PESQ for these conditions (mean of 3 fading seeds; the files are from the first seed):

| Voice | No channel | sotto VW, UHF 52 | DN conv, UHF 52 | DN rep, UHF 52 | sotto VW, VHF 56 |
|---|---:|---:|---:|---:|---:|
| Female (LibriSpeech 198) | 2.16 | 1.69 | 1.35 | 1.19 | 1.82 |
| Male (LibriSpeech 3436) | 2.91 | 2.28 | 1.73 | 1.35 | 2.10 |
| Male (ve9qrp) | 2.85 | 2.50 | 1.84 | 1.48 | 1.94 |

## Encoder against encoder (`encoder/`)

`encoder/<voice>__<condition>__codec2_2400.flac` and `...__sotto_v36.flac` (the page until v3.8) or `...__sotto_v35.flac` (the course) are the same voice sent through the identical chain (sotto VW payload, C4FM softmodem, same channel and fading seed, same receiver). Only the encoder and decoder differ. The conditions are no radio channel; 446 MHz, 60 km/h, 52 dB-Hz; and 146 MHz, 30 km/h, 56 dB-Hz. Scores are in `docs/encoder-v1.md`.

## sotto v3.8 on the page (`v38/`)

The page's Part 1 decodes sotto v3.8 in the browser, so `v38/` holds bitstreams, not audio, for the sotto rows:

| File | What it is |
|---|---|
| `<voice>__<condition>.bit` | the 6-byte frames the softmodem receiver handed the decoder (first fading seed) |
| `<voice>__<condition>.good` | one byte per frame, 0 where the frame failed its CRC (radio conditions only) |
| `<voice>__<condition>__codec2_2400.flac` | Codec 2 2400 through the same chain, same seed |
| `<voice>__original16.flac` | the 16 kHz input (LibriSpeech voices only; ve9qrp exists only at 8 kHz) |

Part 2 of the page (more voices, no radio channel) uses `librispeech_<speaker>_<sex>__No_radio_channel.bit` and its Codec 2 and original16 files: the 8 LibriSpeech dev-clean speakers held out of codebook training (`tools/clean_corpus.py`), the same 12 s clips as the 8 kHz test set. Decode one with `c/sotto_dec -16 X.bit out.raw X.good` (16 kHz raw). Made by `tools/render_page_v38.py`; scores in `docs/results/page_v38.json`. The older `encoder/...__sotto_v36.flac` files are kept for reference.

## Sources

- `male_ve9qrp_15s`: 15 s from `ve9qrp.raw` in the Codec 2 repository (`codec/codec2/raw/`, LGPL-2.1).
- `male_libri3436_15s`: LibriSpeech utterance 3436-172162-0000, from *The Age of Chivalry*, read by Anders Lankford. Trimmed to 15 s.
- `librispeech_<speaker>_<sex>` (`v38/`): the first 12 s of the first utterance of at least 12 s per speaker in LibriSpeech dev-clean (16 kHz, peak-normalised).
- `female_libri198_14s`: LibriSpeech utterance 198-209-0000, from *Sense and Sensibility*, read by Heather Barnett. The full utterance, 13.9 s.

LibriSpeech (V. Panayotov, G. Chen, D. Povey, S. Khudanpur, ICASSP 2015) is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). We used the copies in the librosa example data, downsampled them to 8 kHz (16 kHz for `v38/`) and peak-normalised them. The decoded versions here are derived from those recordings.

## sotto's decoder against FARGAN (`fargan/`)

The page's Part 4. `fargan/<voice>__fargan_v38.flac` is Opus's FARGAN neural vocoder (stock Opus 1.6.1 weights) resynthesising the v3.8 decoder's output from the same `v38/<voice>__No_radio_channel.bit` bits, so its 4-8 kHz bands carry sotto's hiss index; `__fargan_v38_blindhf` guesses those bands from the lower ones instead; `__fargan_ceiling` is FARGAN fed the original speech's own features (not sendable at 2400 bit/s). 16 kHz, made by `tools/fargan_study.py`; scores in `docs/results/fargan_study.json`, write-up in `docs/fargan-study.md`.
