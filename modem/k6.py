"""sotto voice pager softmodem: .sot files -> FSK packets for the Baofeng UV-K6's RF chip,
sent from an ordinary FM transmitter (the IC-7100) fed by a sound card.

  python -m modem.k6 tx msg.sot tx.wav                 audio for the radio (profile D, 1200 baud)
  python -m modem.k6 tx msg.sot tx.wav --phy F         FFSK tones, for a voice / USB audio input
  python -m modem.k6 tx msg.sot tx.wav --phy F --baud 2400 --payload XL --stream
                                                       fastest: FFSK 1200/2400 Hz, 128-byte packets,
                                                       no preamble between them (K6 keys 5, 2, 7)
  python -m modem.k6 rx rx.wav back.sot --good g.u8    decode a recording (or our own tx.wav)
  python -m modem.k6 test test.wav                     spec test packet 0, repeated (calibration)
  python -m modem.k6 cal cal.wav                       modulation-path calibration signal
  python -m modem.k6 fit rec.wav ic7100.json           measure the path from a flat receiver
  python -m modem.k6 tx msg.sot tx.wav --comp ic7100.json   ... and pre-compensate it
  python -m modem.k6 send msg.sot --port /dev/ttyUSB0  key the IC-7100 over CI-V and play
  python -m modem.k6 ctl ctl.wav --phy D --set phy=F frames=XL --also F
                                                       a control packet (spec 13): change the K6's
                                                       receive settings over the air, sent in D
                                                       (where it is now) and again in F

Make .sot files with c/sotto_enc (8 kHz, 16-bit mono raw in) and play them back with
c/sotto_dec back.sot out.raw g.u8 (the good-flag file conceals lost frames). Packet format:
K6 voice-pager FSK packet format v1 (modem/pager.py). Setup notes: docs/k6-pager.md.
"""
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


K6_STORE_FRAMES = 640      # the receiver firmware's default message store (12.8 s)


def cw(text, fs=fsk.FS, wpm=20, tone=800.0, level=0.4):
    """Morse ID as audio (for the station identification an FM data transmission needs)."""
    keys = [0, 0, 0]                      # in dits: 1 = tone
    for ch in text.upper():
        if ch == " ":
            keys += [0] * 4
            continue
        for s in MORSE.get(ch, ""):
            keys += [1] * (1 if s == "." else 3) + [0]
        keys += [0, 0]
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


def build_audio(payloads, a, fs):
    if getattr(a, "prime", False) and payloads:
        payloads = [payloads[0]] + list(payloads)   # the throwaway copy goes first
    # XL: 24 bytes of preamble after the first packet (see pager.Air)
    later = 24 if a.payload == "XL" else 16
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


def _tx_opts(p):
    p.add_argument("--phy", choices="DF", default="D",
                   help="D: direct NRZ 2-FSK (flat input, IC-7100 DATA2 in 9600 bps mode); "
                        "F: FFSK 1200/1800 Hz tones (any audio input); 1200/2400 Hz at --baud 2400")
    p.add_argument("--payload", choices=("L", "XL", "S"), default="L",
                   help="L: 64-byte packets, 8 frames; XL: 128-byte, 20 frames (K6 key 2); "
                        "S: 16-byte (the K6 no longer takes it)")
    p.add_argument("--baud", type=int, choices=(1200, 1600, 1800, 2400), default=1200)
    p.add_argument("--stream", action="store_true",
                   help="packets back to back with only the sync word between them, no per-packet "
                        "preamble (for the K6's stream mode, key 7; about 20%% less air time)")
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
    p.add_argument("--cw-id", metavar="CALL", help="append a Morse ID (FCC 97.119) after the data")
    p.add_argument("--cw-wpm", type=int, default=20)


def _payloads(a, frames):
    prof = pager.PROFILES[a.payload]
    mid = a.msg_id if a.msg_id is not None else msg_id_for(frames)
    pk = prof.pack(frames, mid)
    pk = pk * a.passes
    if getattr(a, "repeat_first", False) and len(pk) > 1:
        pk = pk + pk[:1]
    return pk, mid


def _test_payloads(a):
    frames = b"".join(bytes([f & 0xFF, f >> 8, 0x11, 0x22, 0x33, 0x44]) for f in range(70))
    prof = pager.PROFILES[a.payload]
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
                   help="phy=F|D frames=L|XL baud=1200|2400 auto=0|1 emph=0|1 var=N vol=0..7; "
                        "none: a confirmation only (a good packet that keeps a change)")
    k.add_argument("--action", choices=tuple(pager.CTL_ACTIONS), default="none",
                   help="play: play the stored message; clear: drop it; report: a CFG line")
    k.add_argument("--reg", metavar="ADDR=VALUE",
                   help="write one FD6818 receive register, hex, e.g. 58=73e9 (allowed: 2b 43 44 58 59 5c 5e 72 73 7e 96 9b; 43 on images after 647a60e, 44 73 7e after 691c237, 5e 96 9b after that; 5e also switches to interrupt FIFO reads; "
                        "59 low byte only; 0=0 drops every write); the K6 prints K6 REGS and undoes "
                        "the writes after 60 s without a good packet. --action regs only prints them")
    k.add_argument("--seq", type=int, help="0-255; the K6 applies one seq once (default: from the clock)")
    k.add_argument("--copies", type=int, default=3, help="packets per PHY (default 3)")
    k.add_argument("--also", choices="DF", help="send the same packets again in this PHY after --phy "
                                                 "(when the K6's PHY isn't known)")

    r = sub.add_parser("rx", help="recording -> .sot")
    r.add_argument("input")
    r.add_argument("output", nargs="?")
    r.add_argument("--phy", choices="DF", default="D")
    r.add_argument("--payload", choices=("L", "XL", "S"), default="L")
    r.add_argument("--stream", action="store_true", help="the packets were sent with --stream")
    r.add_argument("--baud", type=int, choices=(1200, 1600, 1800, 2400), default=1200)
    r.add_argument("--invert", action="store_true")
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
    log = lambda *m: print(*m, file=sys.stderr)   # noqa: E731

    if a.cmd in ("tx", "send"):
        frames = open(a.input, "rb").read()
        pk, mid = _payloads(a, frames)
        x = build_audio(pk, a, a.rate)
        n = len(frames) // 6
        if n > K6_STORE_FRAMES:
            log(f"warning: {n} frames is more than the K6 receiver holds by default "
                f"({K6_STORE_FRAMES} frames, {K6_STORE_FRAMES / 50:.1f} s); split the message")
        log(f"{n} frames ({n / 50:.1f} s of speech), msg_id {mid}, {len(pk)} packets, "
            f"{len(x) / a.rate:.1f} s on air (profile {a.payload}, phy {a.phy}, {a.baud} baud"
            f"{', stream' if a.stream else ''})")
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
            parts.append(build_audio([pk] * a.copies, a, a.rate))
        x = np.concatenate(parts)
        if cw_id:
            x = np.concatenate([x, cw(cw_id, a.rate, a.cw_wpm, level=min(a.level, 0.4))])
        _write(a.output, x, a.rate)
        rec = pager.control_reg_record(seq, *reg) if reg else pager.control_record(seq, settings, a.action)
        log(f"control seq {seq} record {rec.hex()}: "
            f"{(len(parts) + 1) // 2 * a.copies} packets, {len(x) / a.rate:.1f} s on air")
        return 0

    if a.cmd == "rx":
        x, fs = _read(a.input, a.iq)
        prof = pager.PROFILES[a.payload]
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
            f"{len(gd) - int(gd.sum())} to conceal")
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
