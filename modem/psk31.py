"""PSK31 (BPSK, 31.25 baud, G3PLX varicode) as audio, for the IC-7100 in USB: the HF test signal of
docs/k6-hf.md. bits: 0 = a phase reversal, 1 = none; characters end with 00; the envelope goes
through zero on a reversal with a raised cosine, as standard PSK31 does. Also a small coherent
decoder, for the tests. SPDX-License-Identifier: MIT"""
import numpy as np

BAUD = 31.25
# G3PLX varicode for the characters a test message needs (no code holds 00; each starts and ends in 1)
VARICODE = {
    " ": "1", "\n": "11101", "\r": "11111", "-": "110101", ".": "1010111", "/": "110101111",
    "0": "10110111", "1": "10111101", "2": "11101101", "3": "11111111", "4": "101110111",
    "5": "101011011", "6": "101101011", "7": "110101101", "8": "110101011", "9": "110110111",
    "A": "1111101", "B": "11101011", "C": "10101101", "D": "10110101", "E": "1110111",
    "F": "11011011", "G": "11111101", "H": "101010101", "I": "1111111", "J": "111111101",
    "K": "101111101", "L": "11010111", "M": "10111011", "N": "11011101", "O": "10101011",
    "P": "11010101", "Q": "111011101", "R": "10101111", "S": "1101111", "T": "1101101",
    "U": "101010111", "V": "110110101", "W": "101011101", "X": "101110101", "Y": "101111011",
    "Z": "1010101101",
}


def bits(text):
    return "".join(VARICODE[c] + "00" for c in text.upper())


def repeat_to(text, seconds, lead_s=1.0, tail_s=0.3):
    """the text repeated to fill about `seconds` with its preamble and tail (whole copies, at least one)"""
    per = len(bits(text)) / BAUD
    n = max(1, int((seconds - lead_s - tail_s) // per))
    return text * n


def modulate(text, fs=48000, tone=1000.0, level=0.5, lead_s=1.0, tail_s=0.3):
    """float audio: lead_s of idle (reversals), the text, tail_s of steady carrier"""
    sym = ["0"] * round(lead_s * BAUD) + list(bits(text)) + ["1"] * round(tail_s * BAUD)
    sps = fs / BAUD
    if abs(sps - round(sps)) > 1e-9:
        raise ValueError("fs must be a whole multiple of 31.25 Hz")
    sps = round(sps)
    phase = [1.0]
    for b in sym:
        phase.append(phase[-1] * (-1.0 if b == "0" else 1.0))
    t = np.arange(sps) / sps
    w = (1 + np.cos(np.pi * t)) / 2                           # 1 -> 0 over a symbol
    base = np.concatenate([phase[k] * w + phase[k + 1] * (1 - w) for k in range(len(sym))])
    n = np.arange(len(base))
    return (level * base * np.cos(2 * np.pi * tone * n / fs)).astype(np.float32)


def demodulate(x, fs=48000, tone=1000.0):
    """coherent decoder for the tests: symbol-centre samples of the baseband, then the varicode"""
    sps = round(fs / BAUD)
    n = np.arange(len(x))
    bb = x * np.cos(2 * np.pi * tone * n / fs)
    k = np.ones(sps // 2) / (sps // 2)
    bb = np.convolve(bb, k, mode="same")
    centres = bb[sps // 2 + sps // 2::sps][:len(x) // sps]      # each symbol's end, where it has settled
    s = np.sign(centres)
    out, run = "", ""
    inv = {v: c for c, v in VARICODE.items()}
    for a, b in zip(s, s[1:]):
        run += "1" if a == b else "0"
        if run.endswith("00"):
            code = run[:-2].lstrip("0")
            if code in inv:
                out += inv[code]
            run = ""
    return out
