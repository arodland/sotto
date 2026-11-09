"""Songs for the K6's MIDI player module (c/k6/mod/midi.c, docs/k6-midi.md).

  python3 tools/k6_midi.py info SONG...               what a song becomes: notes, voices, bytes
  python3 tools/k6_midi.py patch IN.bin OUT.bin SONG...
                                                      CI's mod-midi.bin with these songs in place
                                                      of its own (no compiler needed: hamlet)
  python3 tools/k6_midi.py wav OUT.wav SONG...        what the K6 will play, sample for sample
                                                      (its PWM values at 16 kHz, before the speaker)
  python3 tools/k6_midi.py mid SONG.txt OUT.mid       a text score as a standard MIDI file
  python3 tools/k6_midi.py header                     regenerate c/k6/mod/midi_songs.h from
                                                      DEFAULT_SONGS (tools/midi_songs/)

A SONG is a standard MIDI file (.mid, format 0 or 1; channel 10, the drums, left out) or a text
score (.txt, below). The player has four voices and a 256-sample wavetable, so the tool folds
notes into MIDI 40-95 by octaves (E2-B6: the speaker can't do much below, the harmonics alias
above), keeps at most four notes sounding (the highest and the lowest win: melody and bass), and
puts every time on a grid: 1/24 of a quarter note for a file with one tempo, 8 ms otherwise.

Text score: one item per line.
  title: Ode to Joy           (up to 21 characters on the K6's screen)
  tempo: 116                  quarter notes a minute
  legato: 0.9                 how much of its length a note sounds (default 0.9)
  rh: F#5 F#5 G5 A5 | ...     any other "name:" line is a voice; lines with the same name
                              continue it, and every voice starts at time 0
Lines starting with # are comments. Tokens: a note C4 (middle C), F#5, Bb3; ":len" after it is its length in quarter notes (1/2,
1.5, 3; it carries on to the next notes of the voice until changed); "~ring" after that lets
it sound that many quarters while the voice moves on (pedal); C4+E4+G4 is a chord; r is a
rest; !90 sets the velocity (1-127); | is a bar line and does nothing.

Use public-domain music only: the default songs are our own transcriptions of tunes long out
of copyright. A MIDI file someone sequenced can be a work of its own even when the tune isn't.
SPDX-License-Identifier: MIT
"""
import argparse
import os
import re
import struct
import sys
import wave
from fractions import Fraction

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONG_DIR = os.path.join(ROOT, "tools", "midi_songs")
HEADER = os.path.join(ROOT, "c", "k6", "mod", "midi_songs.h")
# The songs CI's mod-midi.bin carries, in the order it plays them:
# first the old Nokia ring tones that are public domain (Jeff asked for them, 2026-11-09), then
# one more. Kept small so the module (with the 512 B wavetable and a telemetry upload's 696 B)
# still fits the EXPLORE image's 5,376-byte block; the other scores in tools/midi_songs/ go in
# with `patch` (SPI voice images: about 8 KB of songs).
DEFAULT_SONGS = [
    "nokia_tune.txt",
    "fur_elise.txt",
    "mozart_40.txt",
    "bumblebee.txt",
    "valkyrie.txt",
    "ode_to_joy.txt",
]

FS = 16000                 # the module's sample rate
BLK = 16                   # samples a block
NV = 4                     # voices
TOP = 2000                 # PWM period: values 0..TOP, MID silence
MID = TOP // 2
LO = 16
GAP_BLKS = 600             # the module's silence after each song
MAGIC = b"K6MIDI\x01\x00"
VOLUME = 100
MODULE_MAX = 11752         # header + code + tag in the SPI voice images' 11,776-byte block
EXPLORE_MAX = 5376 - 24    # the EXPLORE and lean images' block
SCRATCH_NEED = 512         # the wavetable
NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
HARM = (19000, 7500, 4000, 2000)
QUARTER_SIN = (
    0, 804, 1608, 2410, 3212, 4011, 4808, 5602, 6393, 7179, 7962, 8739, 9512, 10278, 11039, 11793,
    12539, 13279, 14010, 14732, 15446, 16151, 16846, 17530, 18204, 18868, 19519, 20159, 20787, 21403,
    22005, 22594, 23170, 23731, 24279, 24811, 25329, 25832, 26319, 26790, 27245, 27683, 28105, 28510,
    28898, 29268, 29621, 29956, 30273, 30571, 30852, 31113, 31356, 31580, 31785, 31971, 32137, 32285,
    32412, 32521, 32609, 32678, 32728, 32757, 32767)
TOP_INC = [round(2 ** 32 * 440 * 2 ** ((120 + k - 69) / 12) / FS) for k in range(12)]


class Song:
    """notes: [(start s, length s, MIDI note, velocity)]; tempo: quarters a minute if one"""

    def __init__(self, title, notes, tempo=None):
        self.title, self.notes, self.tempo = title, notes, tempo


# ---- text scores -------------------------------------------------------------------------

def parse_len(text):
    return Fraction(text) if "/" in text else Fraction(text.strip())


def parse_note(text):
    m = re.fullmatch(r"([A-G])([#b]?)(-?\d)", text)
    if not m:
        raise ValueError(f"not a note: {text!r}")
    n = NAMES[m.group(1)] + {"#": 1, "b": -1, "": 0}[m.group(2)]
    return 12 * (int(m.group(3)) + 1) + n


def parse_text(src, name="song"):
    title, tempo, legato = name, 120.0, Fraction(9, 10)
    voices = {}
    for raw in src.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):      # a comment: a whole line, as # is also sharp
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        if key == "title":
            title = rest.strip()
        elif key == "tempo":
            tempo = float(rest)
        elif key == "legato":
            legato = parse_len(rest)
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            voices.setdefault(key, []).extend(rest.split())
        else:
            raise ValueError(f"can't read: {raw!r}")
    q = 60.0 / tempo
    notes = []
    for toks in voices.values():
        t, ln, vel = Fraction(0), Fraction(1), 100
        for tok in toks:
            if tok == "|":
                continue
            if tok.startswith("!"):
                vel = max(1, min(127, int(tok[1:])))
                continue
            m = re.fullmatch(r"([^:~]+)(?::([0-9./]+))?(?:~([0-9./]+))?", tok)
            if not m:
                raise ValueError(f"can't read token {tok!r}")
            if m.group(2):
                ln = parse_len(m.group(2))
            ring = parse_len(m.group(3)) if m.group(3) else ln * legato
            if m.group(1) != "r":
                for nt in m.group(1).split("+"):
                    notes.append((float(t) * q, float(ring) * q, parse_note(nt), vel))
            t += ln
    notes.sort()
    return Song(title, notes, tempo)


# ---- standard MIDI files -----------------------------------------------------------------

def _vlq(data, i):
    v = 0
    while True:
        b = data[i]
        i += 1
        v = v << 7 | (b & 0x7F)
        if not b & 0x80:
            return v, i


def parse_midi(data, name="song", drums=False):
    """a standard MIDI file -> Song (notes in seconds, through its tempo map)"""
    if data[:4] != b"MThd":
        raise ValueError("not a MIDI file (no MThd)")
    hlen, fmt, ntrk, div = struct.unpack(">IHHH", data[4:14])
    if div & 0x8000:
        raise ValueError("SMPTE time division isn't supported")
    i = 8 + hlen
    tempos, raw, title = [], [], None
    for tr in range(ntrk):
        while data[i:i + 4] != b"MTrk":
            if i + 8 > len(data):
                raise ValueError("track missing")
            i += 8 + struct.unpack(">I", data[i + 4:i + 8])[0]
        n = struct.unpack(">I", data[i + 4:i + 8])[0]
        j, end, tick, status = i + 8, i + 8 + n, 0, 0
        on = {}
        while j < end:
            d, j = _vlq(data, j)
            tick += d
            b = data[j]
            if b & 0x80:
                status = b
                j += 1
            if status == 0xFF:
                typ = data[j]
                ln, j = _vlq(data, j + 1)
                body = data[j:j + ln]
                j += ln
                if typ == 0x51 and ln == 3:
                    tempos.append((tick, int.from_bytes(body, "big")))
                elif typ == 0x03 and title is None and tr <= 1 and body.strip():
                    title = body.decode("latin-1").strip()
                elif typ == 0x2F:
                    break
                status = 0
            elif status in (0xF0, 0xF7):
                ln, j = _vlq(data, j)
                j += ln
                status = 0
            else:
                kind, ch = status & 0xF0, status & 0x0F
                if kind in (0xC0, 0xD0):
                    j += 1
                    continue
                a, v = data[j], data[j + 1]
                j += 2
                if kind not in (0x80, 0x90) or (ch == 9 and not drums):
                    continue
                key = (ch, a)
                if kind == 0x90 and v:
                    on.setdefault(key, []).append((tick, v))
                elif on.get(key):
                    t0, vel = on[key].pop(0)
                    raw.append((t0, tick, a, vel))
        i = end
    tempos.sort()
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, (0, 500000))

    def sec(tick):
        s, t_prev, us = 0.0, 0, tempos[0][1]
        for t, u in tempos:
            if t > tick:
                break
            s += (t - t_prev) * us / 1e6 / div
            t_prev, us = t, u
        return s + (tick - t_prev) * us / 1e6 / div
    notes = sorted((sec(a), sec(b) - sec(a), n, v) for a, b, n, v in raw if b > a)
    one = len({u for _, u in tempos}) == 1
    return Song(title or name, notes, 60e6 / tempos[0][1] if one else None)


def _wvlq(v):
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    return bytes(reversed(out))


def write_midi(song, ppq=480):
    """Song -> a format 0 standard MIDI file"""
    tempo = song.tempo or 120.0
    ev = []
    for t, d, n, v in song.notes:
        a = round(t * tempo / 60 * ppq)
        b = max(a + 1, round((t + d) * tempo / 60 * ppq))
        ev.append((a, 1, bytes([0x90, n, v])))
        ev.append((b, 0, bytes([0x80, n, 0])))
    ev.sort(key=lambda e: (e[0], e[1]))
    trk = b"\x00\xff\x03" + _wvlq(len(song.title)) + song.title.encode("latin-1")
    trk += b"\x00\xff\x51\x03" + round(60e6 / tempo).to_bytes(3, "big")
    last = 0
    for tick, _, body in ev:
        trk += _wvlq(tick - last) + body
        last = tick
    trk += b"\x00\xff\x2f\x00"
    return b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq) + b"MTrk" + struct.pack(">I", len(trk)) + trk


def load(path, drums=False):
    name = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
    data = open(path, "rb").read()
    if data[:4] == b"MThd":
        return parse_midi(data, name, drums)
    return parse_text(data.decode("utf-8"), name)


# ---- arranging for four voices -----------------------------------------------------------

def fold(n, low=40, high=95):
    while n < low:
        n += 12
    while n > high:
        n -= 12
    return n


def arrange(song, voices=NV, low=40, high=95, transpose=0, max_s=None):
    """Song -> (unit in samples, [(start unit, length units, note, velocity)]): folded, on the
    grid, at most `voices` sounding at once (a note that would be a fifth cuts short an inner
    one if it is the new top or bottom, else it is left out)"""
    unit = round(FS * 60 / song.tempo / 24) if song.tempo else 128
    unit = max(16, min(65535, unit))
    notes = []
    for t, d, n, v in song.notes:
        if max_s is not None and t >= max_s:
            continue
        a = round(t * FS / unit)
        ln = max(1, round(d * FS / unit))
        notes.append([a, ln, fold(n + transpose, low, high), max(8, min(127, (v + 4) // 8 * 8))])
    notes.sort(key=lambda x: (x[0], -x[2]))
    kept, active = [], []
    for nt in notes:
        a = nt[0]
        active = [x for x in active if x[0] + x[1] > a]
        same = [x for x in active if x[2] == nt[2]]
        for x in same:                       # the same note again: the old one stops here
            x[1] = a - x[0]
            active.remove(x)
        if len(active) >= voices:
            pitches = sorted(x[2] for x in active)
            if nt[2] > pitches[-1] or nt[2] < pitches[0]:
                inner = [x for x in active if pitches[0] < x[2] < pitches[-1]] or active
                victim = min(inner, key=lambda x: x[0])
                victim[1] = a - victim[0]
                active.remove(victim)
            else:
                continue
        kept.append(nt)
        active.append(nt)
    return unit, [tuple(x) for x in kept if x[1] > 0]


def _evlq(v):
    if v >= 1 << 21:
        raise ValueError("a length past the 3-byte limit")
    return _wvlq(v)


def encode(title, unit, notes):
    """one song's bytes: unit, title, events"""
    t = re.sub(r"[^ -~]", "?", title)[:21].encode("ascii")
    out = bytearray(struct.pack("<H", unit) + t + b"\0")
    now, vel = 0, 100
    for a, ln, n, v in sorted(notes):
        g = a - now
        while g > 0:
            if g <= 64:
                out.append(0x80 | (g - 1))
                g = 0
            else:
                step = min(g, (1 << 21) - 1)
                out += b"\xc0" + _evlq(step)
                g -= step
        now = a
        if v != vel:
            out += bytes([0xC1, v])
            vel = v
        out.append(n)
        out += _evlq(ln)
    out.append(0xFF)
    return bytes(out)


def table(songs, volume=VOLUME):
    """[(title, unit, notes)] -> the module's song table"""
    if not 1 <= len(songs) <= 255:
        raise ValueError("1 to 255 songs")
    bodies = [encode(*s) for s in songs]
    off = 12 + 2 * len(songs)
    head, offs = bytearray(), []
    for b in bodies:
        offs.append(off)
        off += len(b)
    if off > 0xFFFF:
        raise ValueError("table over 64 KB")
    head = MAGIC + struct.pack("<HBB", off, len(songs), volume) + b"".join(struct.pack("<H", o) for o in offs)
    return bytes(head) + b"".join(bodies)


def read_table(blob):
    """the song table -> (volume, [(title, unit, song bytes)])"""
    if blob[:8] != MAGIC:
        raise ValueError("not a K6 MIDI table")
    total, n, vol = struct.unpack("<HBB", blob[8:12])
    out = []
    for i in range(n):
        o = struct.unpack("<H", blob[12 + 2 * i:14 + 2 * i])[0]
        unit = struct.unpack("<H", blob[o:o + 2])[0]
        e = blob.index(b"\0", o + 2)
        out.append((blob[o + 2:e].decode("ascii"), unit, blob[o:]))
    return vol, out


def patch(code, blob):
    """the module's code with its song table (the last thing in it) replaced"""
    at = code.find(MAGIC)
    if at < 0 or code.find(MAGIC, at + 1) >= 0:
        raise ValueError("not the midi module (no single K6MIDI table)")
    total = struct.unpack("<H", code[at + 8:at + 10])[0]
    if at + total != len(code):
        raise ValueError("the song table isn't the end of the module")
    out = code[:at] + blob
    if 16 + len(out) + 8 + SCRATCH_NEED > MODULE_MAX + 24:
        raise ValueError(f"{len(out)} B: too big for the module block (songs leave out, or use --max-s)")
    return out


# ---- the module, sample for sample -------------------------------------------------------

def sin_q15(ph):
    q, x = ph >> 30, (ph >> 14) & 0xFFFF
    if q & 1:
        x = 0x10000 - x
    i, f = x >> 10, x & 1023
    a = QUARTER_SIN[i]
    v = a + (((QUARTER_SIN[i + 1] - a) * f) >> 10) if i < 64 else a
    return -v if q & 2 else v


def wavetable():
    return np.array([sum((sin_q15(((i * (j + 1)) << 24) & 0xFFFFFFFF) * HARM[j]) >> 15 for j in range(4))
                     for i in range(256)], np.int64)


def _rvlq(b, i):
    v = 0
    for _ in range(3):
        c = b[i]
        i += 1
        v = v << 7 | (c & 0x7F)
        if not c & 0x80:
            break
    return v, i


def render_song(body, vol, wt=None):
    """the CCR4 values midi.c writes for one song (from its unit to its end, no gap)"""
    wt = wavetable() if wt is None else wt
    unit = body[0] | body[1] << 8
    p = body.index(0, 2) + 1
    ph = [0] * NV
    inc = [0] * NV
    left = [0] * NV
    env = [0] * NV
    peak = [0] * NV
    stage = [0] * NV
    wait, vel, end = 0, 100, False
    out = []
    k = np.arange(1, BLK + 1, dtype=np.uint64)
    while True:
        while not end and wait <= 0:
            b = body[p]
            p += 1
            if b < 0x80:
                ln, p = _rvlq(body, p)
                v = next((i for i in range(NV) if not stage[i]), None)
                if v is None:
                    v = 0
                    for i in range(1, NV):
                        if env[i] < env[v]:
                            v = i
                o = (b * 2731) >> 15
                n = b - 12 * o
                inc[v] = TOP_INC[n] >> (10 - o)
                left[v] = ln * unit
                peak[v] = vel << 16
                stage[v] = 1
            elif b < 0xC0:
                wait += ((b & 0x3F) + 1) * unit
            elif b == 0xC0:
                g, p = _rvlq(body, p)
                wait += g * unit
            elif b == 0xC1:
                vel = body[p] & 0x7F
                p += 1
            else:
                end = True
        acc = np.zeros(BLK, np.int64)
        for i in range(NV):
            if stage[i] == 1:
                env[i] += peak[i] >> 2
                if env[i] >= peak[i]:
                    env[i], stage[i] = peak[i], 2
            elif stage[i] == 2:
                env[i] -= env[i] >> 5 if env[i] > peak[i] >> 1 else env[i] >> 10
            elif stage[i] == 3:
                env[i] -= env[i] >> 3
                if env[i] < 4096:
                    env[i], stage[i] = 0, 0
            if stage[i] in (1, 2):
                left[i] -= BLK
                if left[i] <= 0:
                    stage[i] = 3
            g = env[i] >> 12
            phs = (np.uint64(ph[i]) + np.uint64(inc[i]) * k) & np.uint64(0xFFFFFFFF)
            ph[i] = int(phs[-1])
            acc += wt[(phs >> np.uint64(24)).astype(np.int64)] * g
        acc = ((acc >> 11) * vol) >> 13
        out.append(np.clip(MID + acc, LO, TOP - LO))
        wait -= BLK
        if end and not any(stage):
            break
    return np.concatenate(out).astype(np.int64)


def render_table(blob):
    """everything midi.c writes to CCR4 between the speaker going on and the last MID before it
    goes off, with no key pressed: each song, then its 600 ms of silence"""
    vol, songs = read_table(blob)
    wt = wavetable()
    parts = []
    for _, _, body in songs:
        parts.append(render_song(body, vol, wt))
        parts.append(np.full(GAP_BLKS * BLK, MID, np.int64))
    return np.concatenate(parts)


# ---- the generated header ----------------------------------------------------------------

def default_table():
    songs = []
    for f in DEFAULT_SONGS:
        s = load(os.path.join(SONG_DIR, f))
        unit, notes = arrange(s)
        songs.append((s.title, unit, notes))
    return table(songs)


def header_text(blob):
    lines = ["/* midi_songs.h: the song table c/k6/mod/midi.c plays, generated by",
             "   `python3 tools/k6_midi.py header` from tools/midi_songs/ (our own transcriptions of",
             "   public-domain tunes). Don't edit: change the scores and run that again.",
             f"   {len(blob)} bytes. It sits in section .k6tail, the end of the module (module.ld), so",
             "   `k6_midi.py patch` can swap it for other songs. SPDX-License-Identifier: MIT */",
             "__attribute__((section(\".k6tail\"), aligned(4)))",
             "static const uint8_t midi_table[] = {"]
    for i in range(0, len(blob), 16):
        lines.append("  " + " ".join(f"0x{b:02x}," for b in blob[i:i + 16]))
    lines.append("};")
    return "\n".join(lines) + "\n"


# ---- command line ------------------------------------------------------------------------

def _songs(a):
    out = []
    for f in a.songs:
        s = load(f, a.drums)
        unit, notes = arrange(s, low=a.low, high=a.high, transpose=a.transpose, max_s=a.max_s)
        out.append((s.title, unit, notes))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def song_opts(p):
        p.add_argument("--low", type=int, default=40, help="fold notes below this up (MIDI number)")
        p.add_argument("--high", type=int, default=95, help="fold notes above this down")
        p.add_argument("--transpose", type=int, default=0, help="semitones")
        p.add_argument("--max-s", type=float, help="leave out notes after this many seconds")
        p.add_argument("--drums", action="store_true", help="keep channel 10 (it plays as pitches)")
        p.add_argument("--volume", type=int, default=VOLUME, help=f"1-255 (default {VOLUME})")
    p = sub.add_parser("info")
    p.add_argument("songs", nargs="+")
    song_opts(p)
    p = sub.add_parser("patch")
    p.add_argument("inp")
    p.add_argument("out")
    p.add_argument("songs", nargs="+")
    song_opts(p)
    p = sub.add_parser("wav")
    p.add_argument("out")
    p.add_argument("songs", nargs="+")
    song_opts(p)
    p = sub.add_parser("mid")
    p.add_argument("song")
    p.add_argument("out")
    p = sub.add_parser("header")
    p.add_argument("-o", "--out", default=HEADER)
    a = ap.parse_args(argv)

    if a.cmd == "header":
        open(a.out, "w").write(header_text(default_table()))
        print(f"wrote {a.out}")
        return 0
    if a.cmd == "mid":
        open(a.out, "wb").write(write_midi(load(a.song)))
        return 0
    songs = _songs(a)
    blob = table(songs, a.volume)
    if a.cmd == "info":
        for title, unit, notes in songs:
            end = max(x[0] + x[1] for x in notes) * unit / FS if notes else 0
            print(f"{title!r}: {len(notes)} notes, {end:.1f} s, unit {unit} samples "
                  f"({unit * 1000 / FS:.2f} ms), {len(encode(title, unit, notes))} B")
        print(f"table {len(blob)} B; with ~2.4 KB of code: "
              + ("fits every image" if len(blob) + 2400 + SCRATCH_NEED <= EXPLORE_MAX
                 else "SPI voice images only" if len(blob) + 2400 + SCRATCH_NEED <= MODULE_MAX
                 else "too big"))
    elif a.cmd == "patch":
        out = patch(open(a.inp, "rb").read(), blob)
        open(a.out, "wb").write(out)
        print(f"{a.out}: {len(out)} B code ({len(blob)} B of songs), "
              + ("fits the EXPLORE image too" if len(out) + SCRATCH_NEED + 696 <= EXPLORE_MAX else
                 "SPI voice images only (pager-only, lr, vlr, vlrh)"))
    elif a.cmd == "wav":
        x = render_table(blob)
        pcm = ((x - MID) * 16).clip(-32767, 32767).astype("<i2")
        with wave.open(a.out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(FS)
            w.writeframes(pcm.tobytes())
        print(f"{a.out}: {len(x) / FS:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
