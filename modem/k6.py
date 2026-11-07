"""sotto voice pager softmodem: .sot files -> FSK packets for the Baofeng UV-K6's RF chip,
sent from an ordinary FM transmitter (the IC-7100) fed by a sound card.

  python -m modem.k6 tx msg.sot tx.wav                 audio for the radio (profile D, 1200 baud)
  python -m modem.k6 tx msg.sot tx.wav --phy F         FFSK tones, for a voice / USB audio input
                                                       (1800 baud, the K6's F default since 2026-11-05)
  python -m modem.k6 tx msg.sot tx.wav --phy F --baud 2400 --payload XL --stream
                                                       fastest: FFSK 1200/2400 Hz, 128-byte packets,
                                                       no preamble between them (K6 keys 5, 2, 7)
  python -m modem.k6 tx msg.sot tx.wav --phy F --payload XL --codec 1200
                                                       a sotto 1200 message (sotto_enc -1200, 40 ms
                                                       per frame, magic 0xA9): real time at F/1800
  python -m modem.k6 tx msg.sot tx.wav --phy F --payload XL --codec 1200 --parity
                                                       ... with an XOR parity packet after each group
                                                       (a group's one lost packet is rebuilt, spec 14)
  python -m modem.k6 rx rx.wav back.sot --good g.u8    decode a recording (or our own tx.wav)
  python -m modem.k6 test test.wav                     spec test packet 0, repeated (calibration)
  python -m modem.k6 cal cal.wav                       modulation-path calibration signal
  python -m modem.k6 fit rec.wav ic7100.json           measure the path from a flat receiver
  python -m modem.k6 tx msg.sot tx.wav --comp ic7100.json   ... and pre-compensate it
  python -m modem.k6 send msg.sot --port /dev/ttyUSB0  key the IC-7100 over CI-V and play
  python -m modem.k6 mod hello.wav c/k6/mod/hello.c --key-file KEY --phy F --payload XL
                                                       a RAM module the K6 runs once (modem/k6mod.py)
  python -m modem.k6 cw id.wav "AG7EW SOTTO TEST" --wpm 20 --tone 800
                                                       a Morse ID, the CW ID module's test signal
  python -m modem.k6 ctl ctl.wav --phy D --set phy=F frames=XL --also F
                                                       a control packet (spec 13): change the K6's
                                                       receive settings over the air, sent in D
                                                       (where it is now) and again in F

Make .sot files with c/sotto_enc (8 kHz, 16-bit mono raw in) and play them back with
c/sotto_dec back.sot out.raw g.u8 (the good-flag file conceals lost frames); for sotto 1200,
sotto_enc -1200 and sotto_dec -1200 with --codec 1200 here; for sotto 600 / 400, sotto_enc -600
and c/fixed/sotto_vlr_dec_fx 600 with --codec 600. Packet format:
K6 voice-pager FSK packet format v1 (modem/pager.py). Setup notes: docs/k6-pager.md.
"""
from dataclasses import replace
import argparse
import os
import subprocess
import sys
import tempfile
import time
import zlib

import numpy as np
import soundfile as sf

from . import cal, civ, fsk, pager

MORSE = {"A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
         "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
         "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
         "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..", "0": "-----",
         "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
         "7": "--...", "8": "---..", "9": "----.", "/": "-..-."}


K6_STORE_FRAMES = 640      # the receiver firmware's default message store (12.8 s; 25.6 s of sotto 1200)


def cw(text, fs=fsk.FS, wpm=20, tone=800.0, level=0.4, farnsworth=None):
    """Morse ID as audio (for the station identification an FM data transmission needs).
    farnsworth: send the characters at wpm but space them as at this lower speed."""
    gap = 1.0
    if farnsworth and farnsworth < wpm:   # ARRL: stretch the 3- and 7-dit gaps only
        gap = (60 * wpm - 37.2 * farnsworth) / (19 * farnsworth * wpm) / (1.2 / wpm)
    keys = [0, 0, 0]                      # in dits: 1 = tone
    for ch in text.upper():
        if ch == " ":
            keys += [0] * int(round(4 * gap))
            continue
        for s in MORSE.get(ch, ""):
            keys += [1] * (1 if s == "." else 3) + [0]
        keys += [0] * int(round(2 * gap + (gap - 1)))
    dit = int(fs * 1.2 / wpm)
    mask = np.repeat(np.array(keys, float), dit)
    r = int(0.005 * fs)                   # 5 ms edges, no key clicks
    env = np.convolve(mask, np.ones(r) / r, "same")
    return level * env * np.sin(2 * np.pi * tone * np.arange(len(mask)) / fs)


def load_comp(spec):
    if spec in (None, "none"):
        return None
    if spec == "voice":                   # a generic voice FM path, for experiments
        return fsk.Channel(pre_emph=300.0)
    if spec == "preemph":                 # pre-emphasis only (profile F tone levels)
        return fsk.Channel(pre_emph=300.0, hpf=None, lpf=None)
    return cal.Measured.load(spec)


def msg_id_for(frames):
    return zlib.crc32(frames) & 0xFF


def g_audio(payloads, a, fs):
    """PHY G (modem/k6g.py): a control packet (action ifrx, F/1800 XL, --level) asking the K6 for
    a coded 4FSK session as long as the burst, 0.5 s of carrier, then the burst (--g-level)"""
    from . import k6g
    if any(len(p) != k6g.PAYLOAD for p in payloads):
        raise SystemExit("--phy G carries XL packets: add --payload XL")
    lead = int(round(a.g_lead * k6g.BAUD)) // 2 * 2
    r23 = getattr(a, "g_rate", "1/2") == "2/3"
    secs = k6g.burst_seconds(len(payloads), lead, r23)
    seq = a.seq if getattr(a, "seq", None) is not None else int(time.time()) & 0xFF
    ctl = pager.control_packet(seq, None, "ifrx", k6g.PAYLOAD, arg=int(np.ceil(secs / 2)) + 2,
                               val=int(r23))
    c = argparse.Namespace(**vars(a))
    c.phy, c.baud, c.stream, c.prime, c.cw_id, c.payload = "F", 1800, False, False, None, "XL"
    x = build_audio([ctl] * 2, c, fs)
    burst = k6g.audio(k6g.burst_symbols(payloads, lead, r23=r23), fs, a.g_level)
    return np.concatenate([x, np.zeros(int(0.5 * fs)), burst])


def build_audio(payloads, a, fs):
    if a.phy == "G":
        x = g_audio(payloads, a, fs)
        if a.cw_id:
            x = np.concatenate([x, cw(a.cw_id, fs, a.cw_wpm, level=min(a.level, 0.4))])
        return x
    if getattr(a, "prime", False) and payloads:
        payloads = [payloads[0]] + list(payloads)   # the throwaway copy goes first
    # XL: 24 bytes of preamble after the first packet (see pager.Air)
    # XL: 24 bytes at 1200 (timed reads: the chip's late RX-done false-syncs on less); at 1800
    # with interrupt reads 12 was 24/24 and 8 23/24 on air (2026-11-05), so 12 there
    xl_later = 12 if a.baud >= 1800 else 24
    later = getattr(a, "later_preamble", None) or (xl_later if a.payload == "XL" else 16)
    first = getattr(a, "first_preamble", None) or 16
    air = pager.Air(lead_ms=a.lead_ms, preamble_bytes=0 if a.stream else first,
                    later_preamble_bytes=None if a.stream else later)
    bits = air.burst_bits(payloads, a.baud)
    x = fsk.modulate(bits, a.phy, a.baud, fs, a.level, a.invert, comp=load_comp(a.comp),
                     max_gain_db=a.max_gain, tones=getattr(a, "tones", None))
    if a.cw_id:
        x = np.concatenate([x, cw(a.cw_id, fs, a.cw_wpm, level=min(a.level, 0.4))])
    return x


def _write(path, x, fs):
    sf.write(path, np.clip(x, -1, 1).astype(np.float32), fs, subtype="PCM_16")


def _read(path, iq=None):
    if iq:
        from . import sdr
        z = np.fromfile(path, np.complex64)
        return sdr.fm_demodulate(z, iq, fsk.FS, bw_hz=8000.0), fsk.FS
    x, fs = sf.read(path, always_2d=True)
    return x[:, 0], fs


def default_baud(phy):
    """The K6's boot rate for a PHY: F 1800 (images after b1f0492), D 1200; G (coded 4FSK) is
    1200 baud, its control packet F/1800"""
    return 1800 if phy == "F" else 1200


def _tx_opts(p):
    p.add_argument("--phy", choices="DFG", default="D",
                   help="D: direct NRZ 2-FSK (flat input, IC-7100 DATA2 in 9600 bps mode); "
                        "F: FFSK 1200/1800 Hz tones (any audio input); 1200/2400 Hz at --baud 2400; "
                        "G: coded 4FSK for the K6's IF receiver (modem/k6g.py: XL packets, rate-1/2 "
                        "soft Viterbi on the K6; images from 2026-11-07 that aren't lean), after an "
                        "F/1800 control packet that starts its session")
    p.add_argument("--g-level", type=float, default=0.12,
                   help="--phy G: the 4FSK's peak audio (default 0.12, as the IF receiver's bench "
                        "runs; --level stays the control packet's)")
    p.add_argument("--g-rate", choices=("1/2", "2/3"), default="1/2",
                   help="--phy G: the code's rate (default 1/2; 2/3 punctures it, a quarter less "
                        "airtime, about 1.5 dB less margin; images from PR #108 on)")
    p.add_argument("--g-lead", type=float, default=2.0,
                   help="--phy G: the lead's length in seconds (default 2.0). The K6's session starts "
                        "at the control packet (after its burst, before PR #108) and must lock on "
                        "the lead within 6 s")
    p.add_argument("--payload", choices=("L", "XL", "S"), default="L",
                   help="L: 64-byte packets, 8 frames; XL: 128-byte, 20 frames (K6 key 2); "
                        "S: 16-byte (the K6 no longer takes it)")
    p.add_argument("--baud", type=float, default=None,
                   help="bit rate (default: the K6's default for --phy, F 1800 and D 1200; 2400 with "
                        "K6 key 5; others for tests with REG_72 written, e.g. 2000 = 72=50a9, "
                        "F/1200 = 72=3065)")
    p.add_argument("--stream", action="store_true",
                   help="packets back to back with only the sync word between them, no per-packet "
                        "preamble (for the K6's stream mode, key 7; about 20%% less air time)")
    p.add_argument("--codec", choices=tuple(pager.CODECS), default="2400",
                   help="what the .sot holds: 2400 = sotto v3.7 (default), 1200 = sotto 1200 "
                        "(sotto_enc -1200; magic 0xA9, L or XL), 600 / 400 = sotto 600 / 400 "
                        "(sotto_enc -600 / -400; magic 0xAC / 0xAD, L or XL; an image built with K6_CODEC=vlr)")
    p.add_argument("--depth", type=int, choices=pager.DEPTHS, default=pager.GROUP,
                   help="packets per interleave group (default 8): a streaming K6 starts playing once "
                        "a group is in, so 4 starts about twice as soon; a lost packet's frames are "
                        "then 4 slots apart instead of 8 (L and XL; images from 2026-11-05 on)")
    p.add_argument("--short-last", action=argparse.BooleanOptionalAction, default=True,
                   help="a partial last interleave group takes only the packets it fills (seq bit 13; "
                        "about 9%% fewer XL packets). --no-short-last sends the old padded layout, for "
                        "K6 images before 2026-11-07, which drop the short group's packets (they still "
                        "play the full groups)")
    p.add_argument("--trim", action=argparse.BooleanOptionalAction, default=True,
                   help="tx/send: cut dead air from both ends of the message, keeping --trim-keep-ms "
                        "(slots 34 dB or more under the message's level; no K6 change needed)")
    p.add_argument("--trim-keep-ms", type=float, default=60.0,
                   help="silence kept before the first and after the last loud slot (default 60)")
    p.add_argument("--pause", action=argparse.BooleanOptionalAction, default=True,
                   help="tx/send, sotto 2400 and 1200: each inner run of 4+ silent slots goes as its first "
                        "slot and one pause frame, which a K6 from 2026-11-07 fills by replaying that slot "
                        "(older ones play it as one silent slot, so pauses come out short)")
    p.add_argument("--parity", action="store_true",
                   help="send one XOR parity packet after each interleave group (spec 14): a K6 that "
                        "lost one packet of the group rebuilds it exactly instead of concealing its "
                        "frames. Costs 1/depth more air time (12.5%% at depth 8). L and XL; K6 images "
                        "without it ignore the parity packets")
    p.add_argument("--level", type=float, default=0.5, help="peak audio, 0..1 (default 0.5)")
    p.add_argument("--invert", action="store_true", help="flip the bit polarity")
    p.add_argument("--comp", help="pre-compensate: a fit JSON, 'voice' (generic model) or 'preemph'")
    p.add_argument("--max-gain", type=float, default=30.0, help="compensation gain cap, dB (default 30)")
    p.add_argument("--lead-ms", type=float, default=150.0, help="0x55 lead-in after keying (default 150; 400 made the K6 false-sync on the first packet)")
    p.add_argument("--rate", type=int, default=48000, help="audio sample rate (default 48000)")
    p.add_argument("--repeat-first", action=argparse.BooleanOptionalAction, default=False,
                   help="tx/send: send the first packet again at the end (off since --prime; "
                        "2026-11-04 VE9QRP L: repeat-first 492/500, seq 1 lost)")
    p.add_argument("--prime", action=argparse.BooleanOptionalAction, default=False,
                   help="send a throwaway copy of the first packet before the burst. Only for K6 "
                        "images before 8d4be41, which re-arm on an idle no-sync RX-done about 50 ms "
                        "before the first sync (first packet 10/20 without it, 20/20 with it); "
                        "8d4be41 boots with idlekeep=1 and got 20/20 and VE9QRP 500/500 without it "
                        "(2026-11-05). Costs one packet")
    p.add_argument("--tones", type=lambda v: tuple(float(t) for t in v.split(",")), metavar="MARK,SPACE",
                   help="F only: these tones (Hz, bit 1 then bit 0) instead of the usual pair for --baud, "
                        "e.g. 1200,2400 at 1200 baud, to test the K6's tone pair and baud rate apart")
    p.add_argument("--first-preamble", type=int, metavar="BYTES",
                   help="0x55 bytes before the first packet only (default 16, as every packet). "
                        "For tests: the K6 re-arms about 50 ms before the first sync (gap=42-53 "
                        "in every burst on 839924e), and a longer first preamble may give it room")
    p.add_argument("--later-preamble", type=int, metavar="BYTES",
                   help="0x55 bytes before each packet after the first (default XL 12 at 1800 baud and up, "
                        "24 below; L 16); "
                        "for tests of how short a gap the K6 still locks after")
    p.add_argument("--cw-id", metavar="CALL", help="append a Morse ID (FCC 97.119) after the data")
    p.add_argument("--cw-wpm", type=int, default=20)


def _profile(a):
    return pager.profile(a.payload, getattr(a, "codec", "2400"), getattr(a, "depth", pager.GROUP),
                         getattr(a, "short_last", True), getattr(a, "parity", False))


def _trimmed(a, frames):
    """frames as sent: with --trim, dead air cut from both ends; with --pause, inner silences as
    pause frames -> (frames, slots cut at the start, at the end, slots saved by pauses)"""
    codec = getattr(a, "codec", "2400")
    cut0 = cut1 = 0
    if getattr(a, "trim", False):
        frames, cut0, cut1 = pager.trim_silence(frames, codec, getattr(a, "trim_keep_ms", 60.0) / 1000)
    saved = 0
    if getattr(a, "pause", False):
        frames, saved = pager.compact_pauses(frames, codec)
    return frames, cut0, cut1, saved


def _payloads(a, frames):
    prof = _profile(a)
    mid = a.msg_id if a.msg_id is not None else msg_id_for(frames)
    pk = prof.pack(frames, mid)
    drop = {int(x) for x in (getattr(a, "drop", None) or "").split(",") if x}
    if drop:              # leave data packets out on purpose (tests of the K6's parity rebuild)
        pk = [p for p in pk if (q := prof.unpack(p))["parity"] is not None or q["seq"] not in drop]
    pk = pk * a.passes
    if getattr(a, "repeat_first", False) and len(pk) > 1:
        pk = pk + pk[:1]
    return pk, mid


def _test_payloads(a):
    frames = b"".join(bytes([f & 0xFF, f >> 8, 0x11, 0x22, 0x33, 0x44]) for f in range(70))
    prof = replace(_profile(a), short_last=False)     # the spec's full groups: the K6 compares
    pk = prof.pack(frames, 5 if a.payload == "S" else 0x42)
    return pk if a.message else [pk[0]] * a.repeat


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m modem.k6", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tx", help=".sot -> transmit audio")
    t.add_argument("input")
    t.add_argument("output")
    _tx_opts(t)
    t.add_argument("--msg-id", type=int, help="0-255 (default: from the file's CRC)")
    t.add_argument("--passes", type=int, default=1, help="send the message this many times")
    t.add_argument("--drop", metavar="SEQS", help="leave out these data packets, e.g. 3,17 (to test the K6's "
                                                  "--parity rebuild; parity packets are always sent)")

    e = sub.add_parser("test", help="spec test packet 0, repeated (or the whole test message)")
    e.add_argument("output")
    _tx_opts(e)
    e.add_argument("--repeat", type=int, default=20)
    e.add_argument("--message", action="store_true", help="the whole 70-frame test message instead")

    s = sub.add_parser("send", help="key the radio and play a .sot message live")
    s.add_argument("input")
    _tx_opts(s)
    s.add_argument("--msg-id", type=int)
    s.add_argument("--passes", type=int, default=1)
    s.add_argument("--drop", metavar="SEQS", help="as tx --drop")
    s.add_argument("--port", help="CI-V / PTT serial port, e.g. /dev/ttyUSB0 or COM5")
    s.add_argument("--ptt", choices=("civ", "rts", "dtr", "none"), default="civ")
    s.add_argument("--hang-ms", type=float, default=500.0,
                   help="keep PTT on this long after the player exits (default 500; 50 cut the last packet)")
    s.add_argument("--civ-baud", type=int, default=19200)
    s.add_argument("--civ-addr", type=lambda v: int(v, 0), default=civ.IC7100)
    s.add_argument("--set-mode", action="store_true", help="switch the radio to FM-D over CI-V first")
    s.add_argument("--device", help="sound device for the sounddevice package (name or index)")
    s.add_argument("--player", help="play with this command instead, e.g. 'aplay -D plughw:CARD=CODEC'")
    s.add_argument("--max-tx", type=float, default=180.0, help="refuse transmissions longer than this, s")
    s.add_argument("--dry-run", action="store_true", help="print what would happen; don't key or play")

    k = sub.add_parser("ctl", help="control packet: change the K6's receive settings over the air")
    k.add_argument("output")
    _tx_opts(k)
    k.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE",
                   help="phy=F|D frames=L|XL baud=1200|2400 (1200: the PHY's default rate, F 1800 "
                        "on images after b1f0492) auto=0|1 emph=0|1 var=N vol=0..7; "
                        "none: a confirmation only (a good packet that keeps a change)")
    k.add_argument("--action", choices=tuple(pager.CTL_ACTIONS), default="none",
                   help="play: play the stored message; clear: drop it; report: a CFG line; boot: print the bootloader; "
                        "reset: reboot (tools/k6_update.py catches the UPDATE window)")
    k.add_argument("--reg", metavar="ADDR=VALUE",
                   help="write one FD6818 receive register, hex, e.g. 58=73e9 (allowed: 2b 43 44 58 59 5c 5e 72 73 7e 96 9b; 43 on images after 647a60e, 44 73 7e after 691c237, 5e 96 9b after that; 5e also switches to interrupt FIFO reads; "
                        "59 low byte only; 0=0 drops every write); the K6 prints K6 REGS and undoes "
                        "the writes after 60 s without a good packet. --action regs only prints them")
    k.add_argument("--seq", type=int, help="0-255; the K6 applies one seq once (default: from the clock)")
    k.add_argument("--copies", type=int, default=3, help="packets per PHY (default 3)")
    k.add_argument("--also", choices="DF", help="send the same packets again in this PHY after --phy "
                                                 "(when the K6's PHY isn't known)")

    m = sub.add_parser("mod", help="a RAM module (C source) as 0xAB packets: the K6 runs it once")
    m.add_argument("output")
    m.add_argument("source", help="module C source, e.g. c/k6/mod/hello.c (entry: int k6_main(const k6_api_t *)), "
                                  "or its built code (.bin, k6_main first, as the firmware CI artifact's mod-hello.bin)")
    _tx_opts(m)
    m.add_argument("--api-level", type=int, help="the k6_api_t level the module needs (default: the source's "
                   "K6_MODULE_LEVEL, else 1; a K6 refuses a level newer than its own)")
    m.add_argument("--key-file", required=True, help="the 16-byte key the K6 image was built with (modkey)")
    m.add_argument("--serial", type=int, help="must be newer than the last module the K6 ran this boot "
                                              "(default: the clock, in seconds)")
    m.add_argument("--xfer", type=int, help="transfer id 0-255 (default: from the serial)")
    m.add_argument("--only", help="send just these chunks, e.g. 0,3 (the ones a K6 MOD line says are missing); "
                                  "keep --serial and --xfer the same as the first send")
    m.add_argument("--copies", type=int, default=2,
                   help="send every chunk this many times (default 2: on air 2026-11-05 a burst's first "
                        "packet and one more were lost at level 0.3; repeats cost nothing on the K6)")
    m.add_argument("--arq", action="store_true",
                   help="ask for a reply (magic 0xAE): about 2 s after the last packet, on 440.850 only, "
                        "the K6 sends back a report (the chunks it still lacks, or how the run went) "
                        "in the same PHY, then its CW ID; decode a recording with `reports`. "
                        "Images from 2026-11-06 on (API level 4); older ones ignore the packets")
    m.add_argument("--telemetry", type=int, default=0, metavar="N",
                   help="with --arq: send back up to N parts (57 characters each, 12 at most) of the "
                        "module's say lines after it runs, so no serial cable is needed")
    rp = sub.add_parser("reports", help="a recording of the K6 (the IC-7100's receive audio) -> the "
                                        "K6's 0xAF reports after an --arq upload")
    rp.add_argument("input")
    rp.add_argument("--phy", choices="DF", default="F")
    rp.add_argument("--baud", type=float, default=1800)
    rp.add_argument("--invert", action="store_true")
    rp.add_argument("--json", action="store_true", help="one JSON object per report")
    c2 = sub.add_parser("cw", help="a Morse ID as transmit audio: test signal for the K6 CW ID module "
                                   "(c/k6/mod/cwprobe.c); it identifies the station too")
    c2.add_argument("output")
    c2.add_argument("text", help='e.g. "AG7EW SOTTO TEST"')
    c2.add_argument("--wpm", type=float, default=20)
    c2.add_argument("--tone", type=float, default=800.0, help="Hz (default 800, as the hamlet ID keeper)")
    c2.add_argument("--level", type=float, default=0.4)
    c2.add_argument("--farnsworth", type=float, metavar="WPM", help="space the characters as at this speed")
    c2.add_argument("--repeat", type=int, default=1, help="send it this many times")
    c2.add_argument("--gap", type=float, default=3.0, help="seconds between repeats (default 3: the K6 "
                                                          "decoder ends a transmission after 1.2 s)")
    c2.add_argument("--rate", type=int, default=48000)
    mk = sub.add_parser("modkey", help="make a random module key file (32 hex digits, mode 600)")
    mk.add_argument("path")
    ki = sub.add_parser("keyimage", help="write a module key into a K6 image built without one (e.g. CI's)")
    ki.add_argument("input", help="aura-pager-*.bin")
    ki.add_argument("output")
    ki.add_argument("--key-file", required=True)

    b = sub.add_parser("bootdump", help="K6 BOOT lines in a serial log (after ctl --action boot) -> "
                                        "8 KB bootloader image; Baofeng's code, keep it on your machine")
    b.add_argument("log")
    b.add_argument("output")

    r = sub.add_parser("rx", help="recording -> .sot")
    r.add_argument("input")
    r.add_argument("output", nargs="?")
    r.add_argument("--phy", choices="DF", default="D")
    r.add_argument("--payload", choices=("L", "XL", "S"), default="L")
    r.add_argument("--stream", action="store_true", help="the packets were sent with --stream")
    r.add_argument("--codec", choices=tuple(pager.CODECS), default="2400",
                   help="the codec the packets carry (1200: magic 0xA9, 600: 0xAC, 400: 0xAD), as tx")
    r.add_argument("--baud", type=float, default=None, help="default: F 1800, D 1200, as tx")
    r.add_argument("--invert", action="store_true")
    r.add_argument("--expand", action=argparse.BooleanOptionalAction, default=True,
                   help="play pause frames out as the K6 does (N copies of the slot before; default), "
                        "so the .sot is the full-length message")
    r.add_argument("--tones", type=lambda v: tuple(float(t) for t in v.split(",")), metavar="MARK,SPACE",
                   help="F only: the tones sent (Hz, bit 1 then bit 0), as tx --tones, e.g. 1200,1800 at --baud 2400")
    r.add_argument("--iq", type=int, metavar="RATE", help="input is complex64 IQ at RATE samples/s")
    r.add_argument("--good", help="write per-frame good flags (uint8) for c/sotto_dec")
    r.add_argument("--msg-id", type=int, help="which message to write (default: the most complete)")
    r.add_argument("-v", "--verbose", action="store_true", help="list every packet found")

    c = sub.add_parser("cal", help="modulation-path calibration signal")
    c.add_argument("output")
    c.add_argument("--level", type=float, default=0.5)
    c.add_argument("--rate", type=int, default=48000)

    f = sub.add_parser("fit", help="measure the path from a recording of the cal signal")
    f.add_argument("input")
    f.add_argument("output")
    f.add_argument("--iq", type=int, metavar="RATE")
    f.add_argument("--level", type=float, default=0.5, help="the level the cal signal was made with")

    a = ap.parse_args(argv)
    baud_given = getattr(a, "baud", None) is not None
    if hasattr(a, "baud") and not baud_given:
        a.baud = default_baud(a.phy)
    log = lambda *m: print(*m, file=sys.stderr)   # noqa: E731

    if a.cmd in ("tx", "send"):
        frames, cut0, cut1, saved = _trimmed(a, open(a.input, "rb").read())
        pk, mid = _payloads(a, frames)
        x = build_audio(pk, a, a.rate)
        n, fs_ = len(frames) // 6, _profile(a).frame_s
        if cut0 or cut1:
            log(f"trimmed {cut0 * fs_:.2f} s of dead air at the start, {cut1 * fs_:.2f} s at the end "
                f"(--no-trim sends it all)")
        if saved:
            log(f"pause frames stand for {saved} silent slots ({saved * fs_:.2f} s; --no-pause sends them)")
        if n > K6_STORE_FRAMES:
            log(f"warning: {n} frames is more than the K6 receiver holds by default "
                f"({K6_STORE_FRAMES} frames, {K6_STORE_FRAMES * fs_:.1f} s); split the message")
        log(f"{n} frames ({n * fs_:.1f} s of speech), msg_id {mid}, {len(pk)} packets, "
            f"{len(x) / a.rate:.1f} s on air (profile {a.payload}, codec {a.codec}, phy {a.phy}, "
            f"{a.baud} baud{', stream' if a.stream else ''})")
        if a.cmd == "tx":
            _write(a.output, x, a.rate)
            return 0
        return send(x, a, log)

    if a.cmd == "test":
        x = build_audio(_test_payloads(a), a, a.rate)
        _write(a.output, x, a.rate)
        log(f"{len(x) / a.rate:.1f} s of test packets")
        return 0

    if a.cmd == "ctl":
        settings = dict(kv.split("=", 1) for kv in a.set)
        seq = a.seq if a.seq is not None else int(time.time()) & 0xFF
        length = pager.PROFILES[a.payload].payload
        reg = tuple(int(v, 16) for v in a.reg.split("=", 1)) if a.reg else None
        pk = pager.control_packet(seq, settings, a.action, length, reg=reg)
        cw_id, a.cw_id = a.cw_id, None
        parts = []
        for phy in [a.phy] + ([a.also] if a.also and a.also != a.phy else []):
            if parts:      # 1 s of carrier: the K6 applies a change after its burst ends
                parts.append(np.zeros(a.rate))
            a.phy = phy
            if not baud_given:
                a.baud = default_baud(phy)
            parts.append(build_audio([pk] * a.copies, a, a.rate))
        x = np.concatenate(parts)
        if cw_id:
            x = np.concatenate([x, cw(cw_id, a.rate, a.cw_wpm, level=min(a.level, 0.4))])
        _write(a.output, x, a.rate)
        rec = pager.control_reg_record(seq, *reg) if reg else pager.control_record(seq, settings, a.action)
        log(f"control seq {seq} record {rec.hex()}: "
            f"{(len(parts) + 1) // 2 * a.copies} packets, {len(x) / a.rate:.1f} s on air")
        return 0

    if a.cmd == "cw":
        one = cw(a.text, a.rate, a.wpm, a.tone, a.level, a.farnsworth)
        parts = []
        for i in range(a.repeat):
            if i:
                parts.append(np.zeros(int(a.gap * a.rate)))
            parts.append(one)
        x = np.concatenate(parts)
        _write(a.output, x, a.rate)
        log(f"{a.text!r} at {a.wpm:g} wpm, {a.tone:g} Hz, x{a.repeat}: {len(x) / a.rate:.1f} s on air")
        return 0

    if a.cmd == "modkey":
        from . import k6mod
        k6mod.new_key(a.path)
        log(f"wrote {a.path}; build the K6 image with K6_MOD_KEY_FILE={a.path}")
        return 0

    if a.cmd == "keyimage":
        from . import k6mod
        data = k6mod.key_image(open(a.input, "rb").read(), k6mod.read_key(a.key_file))
        with open(a.output, "wb") as f:
            f.write(data)
        log(f"wrote {a.output}: {len(data)} bytes with the key in place; keep it on this machine")
        return 0

    if a.cmd == "mod":
        from . import k6mod
        if a.source.endswith(".bin"):
            code, entry = open(a.source, "rb").read(), 0     # module.ld puts k6_main first
            level = a.api_level or k6mod.LEVEL
        else:
            code, entry = k6mod.build(a.source)
            level = a.api_level or k6mod.source_level(a.source)
        serial = a.serial if a.serial is not None else int(time.time())
        if a.telemetry and not a.arq:
            sys.exit("--telemetry comes back with the reply: add --arq")
        img = k6mod.image(code, entry, serial, k6mod.read_key(a.key_file), level, tlm=a.telemetry)
        pk = k6mod.packets(img, a.xfer if a.xfer is not None else serial & 0xFF,
                           pager.PROFILES[a.payload].payload, arq=a.arq)
        n = len(pk)
        if a.only:
            pk = [pk[int(i)] for i in a.only.split(",")]
        x = build_audio(pk * a.copies, a, a.rate)
        _write(a.output, x, a.rate)
        log(f"module {a.source}: {len(code)} B code, entry +{entry}, API level {level}, serial {serial}, "
            f"xfer {a.xfer if a.xfer is not None else serial & 0xFF}, "
            f"{len(img)} B image in {n} {a.payload} chunks; {len(pk) * a.copies} packets, "
            f"{len(x) / a.rate:.1f} s on air" + ("; reply asked" if a.arq else ""))
        return 0

    if a.cmd == "reports":
        import json
        from . import k6mod
        x, fs = _read(a.input)
        reps = k6mod.reports(x, fs, a.phy, a.baud, a.invert)
        for t, r in reps:
            if a.json:
                print(json.dumps(dict(r, t=round(t, 3))))
            elif r["kind"] == "status":
                print(f"{t:8.2f} s  report {r['seq']}: xfer {r['xfer']} {r['state']}, "
                      f"{r['have']}/{r['count']} chunks, r={r['r']} ({k6mod.RESULTS.get(r['r'], '?')})"
                      + (f", missing {','.join(map(str, r['missing']))}" if r["missing"] else "")
                      + (f", serial {r['serial']} returned {r['ret']} in {r['ms']} ms, "
                         f"{r['stack_free']} B stack free" if r["state"] == "ran" else "")
                      + f" [{r['call']}]")
            else:
                print(f"{t:8.2f} s  report {r['seq']} text {r['part']}/{r['parts'] - 1}: {r['text']!r}")
        st, text = k6mod.collect(reps)
        if st is None:
            log("no K6 report in the recording")
            return 1
        if text and not a.json:
            print(text, end="" if text.endswith("\n") else "\n")
        return 0

    if a.cmd == "bootdump":
        with open(a.log, errors="replace") as f:
            data = pager.boot_dump(f.read())
        with open(a.output, "wb") as f:
            f.write(data)
        print(f"{a.output}: {len(data)} bytes; reset vector {int.from_bytes(data[4:8], 'little'):#010x}, "
              f"stack {int.from_bytes(data[0:4], 'little'):#010x}")
        return 0

    if a.cmd == "rx":
        x, fs = _read(a.input, a.iq)
        prof = _profile(a)
        asm, times = fsk.receive(x, fs, a.phy, a.baud, prof, a.invert, a.stream, a.tones)
        good = sum(g for _, g, _, _ in times)
        invs = sum(1 for _, g, i, _ in times if g and i)
        log(f"{len(times)} packets found, {good} good, {asm.bad} bad CRC"
            + (f"; {invs} good ones arrived inverted (add --invert on tx, or REG_59 bit 10)" if invs else "")
            + (f"; {asm.swapped} good ones had byte-swapped 16-bit words (sender packs its FIFO "
               f"high byte first; the chip sends the low byte first)" if asm.swapped else ""))
        if a.verbose:
            for ts, g, i, amp in times:
                log(f"  {ts:8.3f} s  {'ok ' if g else 'bad'}  amplitude {amp:.4g}"
                    f"{'  inverted' if i else ''}")
        if times:
            log(f"median amplitude {np.median([t[3] for t in times]):.4g} (compare two "
                f"transmitters on the same receiver: equal amplitude = equal deviation)")
        if not asm.messages:
            return 1
        mid = a.msg_id
        if mid is None:
            mid = max(asm.messages, key=lambda m: len(asm.messages[m]["pk"]))
        fr, gd = asm.frames(mid)
        log(f"msg_id {mid}: {len(gd)} frames, {int(gd.sum())} received, "
            f"{len(gd) - int(gd.sum())} to conceal"
            + (f"; {asm.rebuilt} lost packets rebuilt from parity" if asm.rebuilt else ""))
        if a.expand:
            n0 = len(gd)
            fr, gd = pager.expand_pauses(fr, gd, a.codec)
            if len(gd) != n0:
                log(f"pause frames: {n0} slots played out to {len(gd)}")
        if a.output:
            open(a.output, "wb").write(fr)
        if a.good:
            gd.astype(np.uint8).tofile(a.good)
        return 0

    if a.cmd == "cal":
        x = cal.signal(a.level, fsk.FS)
        _write(a.output, fsk.resample(x, fsk.FS, a.rate), a.rate)
        log(f"{len(x) / fsk.FS:.1f} s calibration signal at level {a.level}")
        return 0

    if a.cmd == "fit":
        x, fs = _read(a.input, a.iq)
        m = cal.fit(x, fs, a.level)
        m.save(a.output)
        log("response (0 dB at 1 kHz): " + m.summary())
        log(f"polarity: {'normal' if m.polarity > 0 else 'INVERTED'} (positive audio -> "
            f"{'positive' if m.polarity > 0 else 'negative'} output)")
        if a.iq:
            log(f"1 kHz tone at level {a.level} -> {m.tone_peak / 1000:.2f} kHz peak deviation; "
                f"for 2.5 kHz use --level {a.level * 2500 / m.tone_peak:.2f} (or adjust the radio)")
        return 0
    return 2


def send(x, a, log):
    secs = len(x) / a.rate
    if secs > a.max_tx:
        log(f"refusing: {secs:.0f} s exceeds --max-tx {a.max_tx:.0f} s (split the message, or raise it)")
        return 1
    if a.dry_run:
        log(f"dry run: would key via {a.ptt} on {a.port}, play {secs:.1f} s, unkey")
        return 0
    k = civ.Keyer(a.ptt, a.port, a.civ_baud, a.civ_addr)
    try:
        if a.set_mode:
            k.setup_fm_data()
        k.key(True)
        play(x, a)
        # the audio chain (PipeWire) may still be playing when the player exits
        time.sleep(a.hang_ms / 1000)
    finally:
        k.close()          # always unkeys
    return 0


def play(x, a):
    if a.player:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tx.wav")
            _write(p, x, a.rate)
            subprocess.run(a.player.split() + [p], check=True)
        return
    import sounddevice as sd
    sd.play(np.clip(x, -1, 1).astype(np.float32), a.rate, device=a.device, blocking=True)


if __name__ == "__main__":
    sys.exit(main())
