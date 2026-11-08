"""The Morse RTX module (c/k6/mod/morse.c) on an emulated Cortex-M0, with the K6's keypad and side
keys modelled at the GPIO pins and a modelled operator on the paddles:

  - receive: MCW keying in the tone detector (REG_0B) decodes in real time onto the receive tape
    and line 1, at the speed sent, and comes out in a say line;
  - send: the operator's paddles (iambic B, iambic A, straight key) key the tone oscillator
    (REG_70) inside one tx_on, the over ends with DE AG7EW (or not, if the operator sent AG7EW),
    and every element and gap is a whole number of dots;
  - the transmit rules (as tests/test_k6_txrules.py's, for a module that keys up only when a
    person keys it): 430-450 MHz only, never over a busy channel, inside what it asked tx_on
    for, keys down on every way out with the oscillator off, ident on every over;
  - the keys (EXIT, UP / DOWN, MENU, *) and the screen frames, and what it puts back.

`python3 tests/test_k6_morse.py out.png` draws the emulated screen of a receive-and-send run.
SPDX-License-Identifier: MIT"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, rssi_reg, run  # noqa: E402
from test_k6_txrules import morse, spans_of  # noqa: E402

sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "c", "k6", "mod", "morse.c")
CODE = {"A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
        "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
        "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
        "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..", "0": "-----",
        "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
        "7": "--...", "8": "---..", "9": "----.", "/": "-..-.", "?": "..--..", "=": "-...-"}
SIDE1, SIDE2 = 1 << 14, 1 << 14       # PC14 (dit), PB14 (dah)
PB10, PB11, SPK = 1 << 10, 1 << 11, 1 << 2
MODULE_MAX = 9736                     # header + code + tag in the shared RAM block (PR #142)

_built = {}


def build(**defs):
    """morse.c with #defines ahead of it (MR_RUN_MS=..., MR_MODE=...)"""
    from modem import k6mod
    key = tuple(sorted(defs.items()))
    if key not in _built:
        with tempfile.TemporaryDirectory() as d:
            w = os.path.join(d, "w.c")
            with open(w, "w") as f:
                f.write("#define K6_MODULE_OPT z\n")          # as morse.c asks (k6mod reads w.c)
                f.writelines(f"#define {k} {v}\n" for k, v in defs.items())
                f.write(f'#include "{SRC}"\n')
            _built[key] = k6mod.build(w)
    return _built[key]


def keying(text, wpm, lag_ms=10, t0_ms=1000, gap_after_ms=0):
    """MCW as the tone detector sees it: [(on_us, off_us)] for text at wpm, each mark lag_ms
    longer (the detector's lag)"""
    u = 1200_000 // wpm
    t, out = t0_ms * 1000, []
    for i, ch in enumerate(text):
        if ch == " ":
            t += 4 * u
            continue
        for j, el in enumerate(CODE[ch]):
            n = u if el == "." else 3 * u
            out.append((t + 2000, t + 2000 + n + lag_ms * 1000))   # on 2 ms late, off lag_ms later
            t += n + u
        t += 2 * u
    return out


class Rig:
    """the keypad matrix and side keys at the pins, the TIM1 / GPIOA / RCC registers, and an
    operator. The operator sends text through the paddles: it holds the next element's paddle
    from the moment it hears (the sidetone, TIM1 CCR4) the element before it start, and leaves
    3 dots (a new character) or 7 (a new word) of silence after a character's last element.
    keys: [(from_us, to_us, name)] for EXIT UP DOWN MENU STAR. hold: [(from_us, to_us, 'dit'|
    'dah')] paddle presses as given (squeezes, a straight key) instead of an operator."""

    def __init__(self, text="", wpm=20, start_us=2_000_000, keys=(), hold=(), swap=False):
        self.u = 1200_000 // wpm
        self.els = []                          # (paddle, pause before it: 0 / 3 / 7 dots)
        pause = 0
        for ch in text:
            if ch == " ":
                pause = 7
                continue
            for j, el in enumerate(CODE[ch]):
                self.els.append((el, pause if j == 0 else 0))
                pause = 0
            pause = max(pause, 3)
        self.i, self.press_at = 0, start_us if self.els else None
        self.keys, self.hold, self.swap = keys, hold, swap
        self.regs = {}
        self.rises, self.falls = [], []
        self.st = None

    # the operator, driven by the sidetone
    def on_tone(self, on, us):
        (self.rises if on else self.falls).append(us)
        if on and self.i < len(self.els):      # the element it held for has started
            self.i += 1
            self.press_at = None
            if self.i < len(self.els) and self.els[self.i][1] == 0:
                self.press_at = us             # the next one in the character: hold it now
        elif not on and self.i < len(self.els) and self.press_at is None:
            self.press_at = us + (self.els[self.i][1] - 0) * self.u   # a pause from this fall

    def paddles(self, us):
        dit = dah = False
        if self.i < len(self.els) and self.press_at is not None and us >= self.press_at:
            if self.els[self.i][0] == ".":
                dit = True
            else:
                dah = True
        for a, b, p in self.hold:
            if a <= us < b:
                dit, dah = dit or p == "dit", dah or p == "dah"
        return (dah, dit) if self.swap else (dit, dah)

    def held(self, us):
        return {n for a, b, n in self.keys if a <= us < b}

    def rows(self):
        return self.st.get("rows", 0) if self.st else 0

    # the pins
    def cols_low(self, st):
        self.st = st
        us = st["us"]
        s1, s2 = self.paddles(us)
        k, r = self.held(us), self.rows()
        c = 0
        if s1 or ("UP" in k and r & PB11):
            c |= 2
        if s2 or ("DOWN" in k and r & PB11):
            c |= 8
        if "MENU" in k and r & PB11:
            c |= 1
        if ("EXIT" in k and r & PB11) or ("STAR" in k and r & PB10):
            c |= 4
        return c

    def pb_idr(self, st):
        return 0x10 | (0 if self.cols_low(st) & 8 else SIDE2)

    def gpiob(self, off, value, st):
        st.setdefault("rows", 0)
        if off == 0x28:                        # BRR
            st["rows"] |= value & (PB10 | PB11)
            if value & SPK:
                st["spk"] = 0
        elif off == 0x18:                      # BSRR
            st["rows"] &= ~(value & (PB10 | PB11))
            if value & SPK:
                st["spk"] = 1
        else:
            raise AssertionError(f"GPIOB+{off:#x} written")

    def mmio(self):
        def reg_r(base):
            def r(uc, off, size, st):
                a = base + off
                if a == 0x48000810:            # GPIOC IDR: PC13 PC14
                    c = self.cols_low(st)
                    return 0xFFFF & ~((c & 1) << 13) & ~(((c >> 1) & 1) << 14)
                if a == 0x48001410:            # GPIOF IDR: PF6
                    return 0xFFFF & ~(((self.cols_low(st) >> 2) & 1) << 6)
                if a == 0x48000414:            # GPIOB ODR: the amplifier off
                    return 0x0F11
                return self.regs.get(a, 0)
            return r

        def reg_w(base):
            def w(uc, off, size, value, st):
                a = base + off
                if a == 0x40012C40:            # TIM1 CCR4: the sidetone
                    if (value > 0) != (self.regs.get(a, 0) > 0):
                        self.on_tone(value > 0, st["us"])
                self.regs[a] = value
            return w
        return [(b, 0x400, reg_r(b), reg_w(b)) for b in (0x48000000, 0x48000800, 0x48001400,
                                                         0x40012C00, 0x40021000)]


def go(rig, regs=START, level=7, rx_hz=440_850_000, tx=None, defs=None, lcd=None, led=None):
    defs = {"MR_RUN_MS": 30000, **(defs or {})}
    tx = {} if tx is None else tx
    lcd = {} if lcd is None else lcd
    ret, said, regs, writes = run(regs, code=build(**defs), level=level, tx=tx, rx_hz=rx_hz,
                                  mmio=rig.mmio(), gpiob=rig.gpiob, pb_idr=rig.pb_idr, lcd=lcd,
                                  led=led, count=400_000_000)
    return ret, said, regs, tx, lcd


def units(spans, u):
    """each span in dots: marks of 1 or 3 and the gaps inside a character (1) whole to 3 ms;
    the gaps between characters are the operator's (and a screen redraw's) to stretch"""
    out = []
    for on, us in spans:
        n = round(us / u)
        if on or n < 3:
            assert abs(us - n * u) < 3000 and (n in (1, 3) if on else n == 1), (on, us, n)
        out.append(n)
    return out


@need_clang
def test_builds_level_6_and_fits():
    from modem import k6mod
    code, entry = k6mod.build(SRC)
    assert k6mod.source_level(SRC) == 6 and entry == 0
    assert 16 + len(code) + 8 <= MODULE_MAX, len(code)
    assert 16 + len(code) + 8 + 1024 <= 9760    # and its tapes and state in the scratch after it


@need_clang
def test_receives_and_draws_the_tape():
    spans = keying("CQ DE K7ABC", 18)
    led = []
    regs = {**START, 0x0B: lambda us: 0xD200 if any(a <= us < b for a, b in spans) else 0xC000}
    ret, said, regs_end, tx, lcd = go(Rig(), regs=regs, defs={"MR_RUN_MS": 12000}, led=led)
    assert ret == 0 and "log" not in tx
    assert said[0] == "morse 20wpm IB tone=700 rx=440850000 tx=on", said
    assert "rx 18wpm CQ DE K7ABC" in said, said
    last = lcd["frames"][-1]
    assert last["text"][1].strip() == "CQ DE K7ABC", last["text"]
    assert last["text"][0].startswith("CW 20wpm IB r18") and last["text"][0].rstrip().endswith("RX")
    rx_tape = last["wf"][:14]
    assert sum(bin(b).count("1") for row in rx_tape for b in row) > 150
    assert not any(any(r) for r in last["wf"][15:])      # nothing sent
    # the tone detector set up (16 tones) and switched off at the end, the speaker audio back
    assert regs_end[0x24] == 0 and regs_end.get(0x47, 0) == 0
    assert led and led[0][1] == 1 and led[-1][1] == 0
    # a redraw (30 ms here) never hides a key change: each starts just after one, or in a quiet
    edges = sorted(t for sp in spans for t in sp)
    for f in lcd["frames"][1:]:
        assert not any(f["us"] < t < f["us"] + 30_000 for t in edges), f["us"]


@need_clang
@pytest.mark.parametrize("wpm", [10, 14, 25, 32])
def test_receive_follows_the_speed(wpm):
    """the decoder starts at 20 wpm and follows the sender: by the second CQ it has the speed"""
    spans = keying("CQ CQ DE K7ABC", wpm)
    end = spans[-1][1] // 1000 + 3000
    regs = {**START, 0x0B: lambda us: 0xD200 if any(a <= us < b for a, b in spans) else 0xC000}
    ret, said, _, tx, lcd = go(Rig(), regs=regs, defs={"MR_RUN_MS": end})
    rx = [s for s in said if s.startswith("rx ")]
    assert len(rx) == 1 and rx[0].endswith("CQ DE K7ABC"), said
    assert abs(int(rx[0].split()[1][:-3]) - wpm) <= 1, rx


@need_clang
def test_receive_ignores_detector_flicker():
    """1-6 ms flickers of the tone detector, on marks and in the gaps, change nothing"""
    import random
    rnd = random.Random(5)
    spans = keying("CQ DE K7ABC", 18)
    flick = [(t, t + rnd.randint(1, 6) * 1000) for t in sorted(rnd.sample(range(1_000_000, 9_000_000, 997), 120))]

    def b(us):
        on = any(a <= us < e for a, e in spans)
        if any(a <= us < e for a, e in flick):
            on = not on
        return 0xD200 if on else 0xC000
    ret, said, *_ = go(Rig(), regs={**START, 0x0B: b}, defs={"MR_RUN_MS": 12000})
    assert "rx 18wpm CQ DE K7ABC" in said, said


@need_clang
@pytest.mark.parametrize("mode", [0, 1])
def test_paddles_send_with_the_ident(mode):
    rig = Rig("CQ")
    ret, said, regs, tx, lcd = go(rig, defs={"MR_MODE": mode})
    log = tx["log"]
    names = [n for n, *_ in log if n != "tx_pa"]
    assert names == ["tx_on", "tx_off"], names
    asked, t_on, t_off = log[0][1], log[0][2], log[-1][2]
    assert asked == 60000 and (t_off - t_on) / 1000 < asked
    assert 2_000_000 <= t_on < 2_100_000                # the first touch keys up
    sp = spans_of(tx, t_off)
    assert morse(sp) == "CQ DE AG7EW", morse(sp)
    units(sp[1:], 60_000)                                # (the first span: the key-up's lead)
    assert regs.get(0x70, 0) == 0 and regs.get(0x71, 0) == 0
    keyed = {r for r, v, us in tx["w"] if t_on <= us <= t_off}
    assert keyed <= {0x70, 0x71}, keyed
    assert ret == 1 and said[-1] == "morse end overs=1"
    over = [s for s in said if s.startswith("tx ")]
    assert len(over) == 1 and over[0].startswith("tx 20wpm ms=") and over[0].endswith("id=auto CQ DE AG7EW")
    last = lcd["frames"][-1]
    assert last["text"][5].strip() == "CQ DE AG7EW" and last["text"][0].rstrip().endswith("RX")
    assert sum(bin(b).count("1") for row in last["wf"][15:] for b in row) > 150
    # the operator heard each element on the sidetone as it went out on the air
    tone_on = [us for r, v, us in tx["w"] if r == 0x70 and v]
    assert len(rig.rises) == len(tone_on) and all(abs(a - b) < 200 for a, b in zip(rig.rises, tone_on))


@need_clang
def test_own_ident_needs_no_auto_ident():
    tx = {}
    ret, said, *_ = go(Rig("CQ DE AG7EW K"), tx=tx)
    t_off = tx["log"][-1][2]
    assert morse(spans_of(tx, t_off)) == "CQ DE AG7EW K"
    assert any(s.endswith("id=own CQ DE AG7EW K") for s in said), said


@need_clang
@pytest.mark.parametrize("mode,want", [(0, ".-."), (1, ".-")])
def test_squeeze_iambic_b_and_a(mode, want):
    """dit then dah squeezed, both let go during the dah: B adds the dit it latched, A doesn't.
    Off 70 cm it's practice: the sidetone only, no transmitter"""
    u, t0 = 60_000, 2_000_000
    rig = Rig(hold=((t0, t0 + 3 * u, "dit"), (t0 + 2000, t0 + 3 * u, "dah")))
    ret, said, regs, tx, lcd = go(rig, rx_hz=146_520_000, defs={"MR_MODE": mode, "MR_RUN_MS": 8000})
    assert "log" not in tx and said[0].endswith("tx=off")
    got = "".join("." if round((f - r) / u) == 1 else "-" for r, f in zip(rig.rises, rig.falls))
    assert got == want
    assert lcd["frames"][-1]["text"][0].rstrip().endswith("PR")


@need_clang
def test_straight_key_follows_the_hand():
    t0, ms = 2_000_000, 1000
    # the first touch only keys up (tx_on takes 1.4-1.9 s on the radio): a tap (an E, cut short
    # by the key-up), then the hand: .-. (R) with the hand's own lengths
    t1 = t0 + 400 * ms
    hold = [(t0, t0 + 50 * ms, "dit"), (t1, t1 + 90 * ms, "dit"), (t1 + 200 * ms, t1 + 420 * ms, "dah"),
            (t1 + 500 * ms, t1 + 560 * ms, "dit")]
    rig = Rig(hold=hold)
    ret, said, regs, tx, lcd = go(rig, defs={"MR_MODE": 2, "MR_RUN_MS": 12000})
    t_on = tx["log"][0][2]
    ups = [(us, bool(v)) for r, v, us in tx["w"] if r == 0x70 and us > t1]
    marks = [b[0] - a[0] for a, b in zip(ups, ups[1:]) if a[1] and not b[1]]
    # the hand's marks, then the ident's: each within the 5 ms debounce and a 1 ms poll
    assert [round(m / 1000) for m in marks[:3]] == pytest.approx([90, 220, 60], abs=2)
    assert any(s.startswith("tx 20wpm") and s.endswith("id=auto E R DE AG7EW") for s in said), said
    assert lcd["frames"][-1]["text"][0][9:11] == "SK"


@need_clang
@pytest.mark.parametrize("hz", [146_520_000, 420_500_000, 451_000_000, 118_100_000])
def test_never_keys_up_off_70cm(hz):
    tx = {}
    ret, said, *_ = go(Rig("E"), rx_hz=hz, tx=tx, defs={"MR_RUN_MS": 9000})
    assert "log" not in tx and ret == 0


@need_clang
def test_keys_up_anywhere_in_70cm():
    tx = {}
    ret, said, *_ = go(Rig("E"), rx_hz=446_000_000, tx=tx, defs={"MR_RUN_MS": 9000})
    assert tx["log"][0][0] == "tx_on" and ret == 1


@need_clang
def test_never_over_a_busy_channel():
    tx = {}
    regs = {**START, 0x67: lambda us: rssi_reg(-60)}
    ret, said, *_ = go(Rig("E"), regs=regs, tx=tx, defs={"MR_RUN_MS": 9000})
    assert "log" not in tx and "morse channel busy: practice" in said


@need_clang
def test_needs_level_6():
    tx = {}
    ret, said, *_ = run(START, code=build(MR_RUN_MS=3000), level=5, tx=tx)
    assert ret == -1 and "log" not in tx and not said


@need_clang
def test_older_image_gets_a_short_over():
    tx = {}
    go(Rig("E"), level=6, tx=tx, defs={"MR_RUN_MS": 9000})
    assert tx["log"][0][1] == 18000


@need_clang
def test_every_way_out_keys_down():
    # tx_on refused: practice from then on, never asked again
    tx = {"on_ret": -1}
    ret, said, regs, *_ = go(Rig("E E"), tx=tx, defs={"MR_RUN_MS": 12000})
    assert [n for n, *_ in tx["log"]] == ["tx_on"] and "morse tx refused: practice" in said
    # the core's time limit: the PA dead 1 s into the over
    tx = {"dead_us": 3_000_000}
    ret, said, regs, *_ = go(Rig("CQ CQ CQ"), tx=tx, defs={"MR_RUN_MS": 12000})
    names = [n for n, *_ in tx["log"] if n != "tx_pa"]
    assert names == ["tx_on", "tx_off"] and tx["log"][-1][2] < 3_150_000
    assert regs.get(0x70, 0) == 0 and "morse tx cut by the core's limit" in said
    # EXIT while keyed (the paddles up after CQ): the ident goes out at once, then key-down, then
    # the module ends
    tx = {}
    rig = Rig("CQ", keys=((4_000_000, 4_200_000, "EXIT"),))
    ret, said, regs, *_ = go(rig, tx=tx, defs={"MR_RUN_MS": 0})
    assert morse(spans_of(tx, tx["log"][-1][2])) == "CQ DE AG7EW"
    assert tx["log"][-1][0] == "tx_off" and said[-1] == "morse end overs=1" and ret == 1
    assert tx["log"][-1][2] < 4_200_000 + 6_000_000          # no 3 s hang first


@need_clang
def test_over_ends_in_time_for_the_ident():
    """the paddles held for ever: they stop in time for the ident inside the over's limit"""
    tx = {}
    rig = Rig(hold=((2_000_000, 40_000_000, "dit"),))
    go(rig, level=6, tx=tx, defs={"MR_RUN_MS": 45000})
    log = tx["log"]
    t_on, t_off = log[0][2], log[-1][2]
    assert log[0][1] == 18000 and (t_off - t_on) / 1000 < 18000
    assert morse(spans_of(tx, t_off)).endswith("DE AG7EW")


@need_clang
def test_keys_change_speed_mode_and_paddles():
    ms = 1000
    keys = ((1000 * ms, 1100 * ms, "UP"), (1500 * ms, 1600 * ms, "UP"), (2000 * ms, 2100 * ms, "MENU"),
            (2500 * ms, 2600 * ms, "STAR"))
    rig = Rig(keys=keys, hold=((4000 * ms, 4030 * ms, "dit"),))
    ret, said, regs, tx, lcd = go(rig, rx_hz=146_520_000, defs={"MR_RUN_MS": 8000})
    title = lcd["frames"][-1]["text"][0]
    assert title.startswith("CW 22wpm IA"), title
    # swapped: Side 1 is now the dah paddle (3 dots of 22 wpm: 163 ms)
    assert len(rig.rises) == 1 and abs(rig.falls[0] - rig.rises[0] - 3 * 54_000) < 3000


@need_clang
def test_puts_back_what_it_changed():
    rig = Rig("E")
    ret, said, regs, tx, lcd = go(rig, defs={"MR_RUN_MS": 9000})
    r = rig.regs
    assert r.get(0x40012C00, 0) == 0 and r.get(0x40012C44, 0) & 0x8000 == 0   # TIM1 off, MOE off
    assert r.get(0x48000000, 0) == 0 and r.get(0x40021018, 0) == 0          # PA11, the clock
    assert rig.st["spk"] == 0 and rig.st["rows"] == 0                        # amplifier, rows idle


def draw(frames, path, fonts="/mnt/project-files/k6-fonts/fonts.txt", S=4):
    """emulated frames as one PNG, pixel for pixel as the firmware draws them (draw_mod_screen:
    an inverted 11-row title bar, FONT_6X10 lines at baselines 9, 19 .. 59, the module's rows
    from y = 21), with the glyphs from the K6 fonts dump (k6-fonts/fonts.txt)"""
    from PIL import Image, ImageDraw
    g, fw, fh, bl = {}, 6, 10, 7
    cur = None
    for ln in open(fonts):
        p = ln.split()
        if p[0] == "FONT":
            cur = p[1]
            if cur == "FONT_6X10":
                fw, fh, bl = int(p[2]), int(p[3]), int(p[5])
        elif cur == "FONT_6X10":
            g[chr(int(p[1]))] = p[2]
    on, off, edge = (0x1c, 0x24, 0x1a), (0x9a, 0xa8, 0x8c), (0x8e, 0x9c, 0x80)
    pad = 8
    img = Image.new("RGB", (len(frames) * (128 * S + pad) + pad, 64 * S + 2 * pad), (40, 40, 40))
    d = ImageDraw.Draw(img)
    for n, f in enumerate(frames):
        fb = [[0] * 128 for _ in range(64)]
        for y in range(11):
            fb[y] = [1] * 128
        for i, t in enumerate(f["text"]):
            if f["wf"] and 2 <= i < 5:
                continue
            for k, ch in enumerate(t):
                gl = g.get(ch, g.get("?"))
                for r in range(fh):
                    for c in range(fw):
                        x, y = 1 + k * fw + c, 9 + 10 * i - bl + r
                        if 0 <= x < 128 and 0 <= y < 64 and gl[r * fw + c] == "1":
                            fb[y][x] = 0 if i == 0 else 1
        for r, row in enumerate(f["wf"]):
            for x in range(128):
                if row[x >> 3] & (0x80 >> (x & 7)):
                    fb[21 + r][x] = 1
        ox = pad + n * (128 * S + pad)
        for y in range(64):
            for x in range(128):
                x0, y0 = ox + x * S, pad + y * S
                d.rectangle([x0, y0, x0 + S - 1, y0 + S - 1], fill=on if fb[y][x] else off)
                d.line([x0 + S - 1, y0, x0 + S - 1, y0 + S - 1], fill=on if fb[y][x] else edge)
    img.save(path)


if __name__ == "__main__":
    # K7ABC calls CQ at 18 wpm; the K6's operator answers on the paddles at 20 wpm
    spans = keying("CQ CQ DE K7ABC K", 18)
    regs = {**START, 0x0B: lambda us: 0xD200 if any(a <= us < b for a, b in spans) else 0xC000}
    rig = Rig("K7ABC DE AG7EW KN", start_us=spans[-1][1] + 2_500_000)
    ret, said, _, tx, lcd = go(Rig() if "--rx" in sys.argv else rig, regs=regs, defs={"MR_RUN_MS": 34000})
    for ln in said:
        print("K6 MOD say", ln)
    fr = lcd["frames"]
    pick = [max((f for f in fr if not f["text"][5].strip() or f["text"][5].startswith("paddles")),
                key=lambda f: f["us"]),
            next(f for f in fr if f["text"][5].strip().startswith("K7ABC DE AG")),
            fr[-1]]
    for f in pick:
        print(f["us"] // 1000, "ms:", " | ".join(f["text"]))
    draw(pick, next((a for a in sys.argv[1:] if a.endswith(".png")), "morse.png"))
