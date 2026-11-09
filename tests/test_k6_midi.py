"""The K6 MIDI player module (c/k6/mod/midi.c) on an emulated Cortex-M0 against a modelled TIM1,
and tools/k6_midi.py: its text scores and MIDI files, the four-voice arrangement, the song table,
the patch of a built module, and its model of the player, which must match the module's PWM
values sample for sample. SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, run  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_midi  # noqa: E402

SRC = os.path.join(ROOT, "c", "k6", "mod", "midi.c")
TIM1, RCC, GPIOA = 0x40012C00, 0x40021000, 0x48000000
AT_START = {"rcc": {0x18: 0x0000_0201}, "gpioa": {0x00: 0x2800_0000, 0x08: 0x0C00_0000, 0x24: 0x0000_0110}}
# a short test song: a scale's start, a five-note chord (the tool keeps four), a held note
TEST_TXT = """title: Test tune
tempo: 240
mel: C5:1/2 D5 E5 | C4+E4+G4+C5+E5:1 | !60 A5:2
"""


def emulate(code, keys=(), level=7, tim_busy=False, spare=1024):
    """run the module: (ret, said, chip writes, log) with log = {"ccr4": [values written],
    "spk": [(on, index in ccr4)], "tim", "rcc", "gpioa": theirs at the end, "lcd", "led"}"""
    log = {"ccr4": [], "spk": [], "tim": {0x00: int(tim_busy)}, "updates": 0,
           "rcc": dict(AT_START["rcc"]), "gpioa": dict(AT_START["gpioa"]), "armed": False,
           "lcd": {}, "led": []}

    def tim_r(uc, off, size, st):
        if off == 0x10 and log["tim"].get(0x00, 0) & 1:
            if not log["armed"]:
                log["armed"] = True
                return 0
            log["updates"] += 1
            st["us"] += 62 + (log["updates"] & 1)   # 62.5 us an update, in whole us
            return 1
        return log["tim"].get(off, 0)

    def tim_w(uc, off, size, value, st):
        if off == 0x10:
            log["armed"] = False
        if off == 0x40:
            log["ccr4"].append(value)
        log["tim"][off] = value

    def reg_r(name):
        return lambda uc, off, size, st: log[name].get(off, 0)

    def reg_w(name):
        def w(uc, off, size, value, st):
            log[name][off] = value
        return w

    def gpiob(off, value, st):
        assert value == 1 << 2 and off in (0x18, 0x28), (hex(off), value)
        log["spk"].append((off == 0x18, len(log["ccr4"])))

    mmio = [(TIM1, 0x400, tim_r, tim_w), (RCC, 0x400, reg_r("rcc"), reg_w("rcc")),
            (GPIOA, 0x400, reg_r("gpioa"), reg_w("gpioa"))]
    ret, said, regs, writes = run({**START, 0x47: 0x6140}, mmio=mmio, keys=keys, gpiob=gpiob, level=level,
                                  lcd=log["lcd"] if level >= 6 else None, led=log["led"], code=code,
                                  spare=spare, count=2_000_000_000)
    return ret, said, writes, log


def played(log):
    """the values written while the amplifier was on, less the one queued before it went off"""
    (on, a), (off, b) = log["spk"]
    assert on and not off
    return np.array(log["ccr4"][a:b - 1], np.int64)


def songs_table(*texts, **kw):
    out = []
    for t in texts:
        s = k6_midi.parse_text(t)
        unit, notes = k6_midi.arrange(s, **kw)
        out.append((s.title, unit, notes))
    return k6_midi.table(out)


@pytest.fixture(scope="module")
def built():
    from modem import k6mod
    return k6mod.build(SRC)


def with_songs(built, blob):
    code, entry = built
    return k6_midi.patch(code, blob), entry


@need_clang
def test_builds_small_with_the_table_last(built):
    from modem import k6mod
    code, entry = built
    assert k6mod.source_level(SRC) == 2 and entry == 0
    blob = k6_midi.default_table()
    assert code.endswith(blob)
    # the EXPLORE image's block: header, code, tag, the wavetable and a telemetry upload's 696 B
    assert 16 + len(code) + 8 + 512 + 696 <= 5376, len(code)


def test_header_is_up_to_date():
    with open(k6_midi.HEADER) as f:
        assert f.read() == k6_midi.header_text(k6_midi.default_table()), \
            "run python3 tools/k6_midi.py header"


def test_scores_read_and_go_through_a_midi_file():
    for name in sorted(os.listdir(k6_midi.SONG_DIR)):
        s = k6_midi.load(os.path.join(k6_midi.SONG_DIR, name))
        assert s.notes and len(s.title) <= 21, name
        m = k6_midi.parse_midi(k6_midi.write_midi(s))
        assert m.title == s.title and abs(m.tempo - s.tempo) < 0.01
        assert k6_midi.arrange(m) == k6_midi.arrange(s), name


def test_text_score_tokens():
    s = k6_midi.parse_text("title: T\ntempo: 60\nlegato: 1\na: C4 D#4:1/2 r Bb3+F4:2~3 !50 C-1\n")
    assert s.notes == [(0.0, 1.0, 60, 100), (1.0, 0.5, 63, 100), (2.0, 3.0, 58, 100),
                       (2.0, 3.0, 65, 100), (4.0, 2.0, 0, 50)]


def test_arrangement_keeps_four_voices_melody_and_bass():
    s = k6_midi.Song("x", [(0.0, 1.0, n, 100) for n in (48, 52, 55, 60, 64, 67)], 120.0)
    unit, notes = k6_midi.arrange(s)
    assert unit == round(16000 * 0.5 / 24)
    assert sorted(n for _, _, n, _ in notes) == [48, 52, 55, 67]   # inner notes gave way to the new bottoms
    s = k6_midi.Song("x", [(0.0, 1.0, 20, 127), (0.0, 1.0, 110, 1)], 120.0)
    assert sorted((n, v) for _, _, n, v in k6_midi.arrange(s)[1]) == [(44, 127), (86, 8)]


def test_table_round_trip_and_patch_refuses_others():
    blob = songs_table(TEST_TXT, TEST_TXT.replace("Test tune", "Again"))
    vol, songs = k6_midi.read_table(blob)
    assert vol == k6_midi.VOLUME and [t for t, _, _ in songs] == ["Test tune", "Again"]
    with pytest.raises(ValueError):
        k6_midi.patch(b"\0" * 100, blob)
    with pytest.raises(ValueError):
        k6_midi.patch(b"\0" * 100 + blob + b"\0", blob)
    big = k6_midi.table([("big", 16, [(i * 4, 2, 60, 100) for i in range(4000)])])
    with pytest.raises(ValueError):
        k6_midi.patch(b"\0" * 100 + blob, big)


@need_clang
def test_plays_sample_for_sample_as_the_model(built):
    blob = songs_table(TEST_TXT, TEST_TXT.replace("Test tune", "Again"))
    ret, said, writes, log = emulate(with_songs(built, blob))
    assert ret == 2, said
    assert said[0] == f"midi songs=2 bytes={len(blob)} vol={k6_midi.VOLUME}"
    assert said[1:3] == ["midi play 1/2 Test tune", "midi play 2/2 Again"]
    assert said[-1] == "midi done songs=2 notes=16 late=0"
    assert writes == [(0x47, 0x6040), (0x47, 0x6140)]     # the chip's AF muted while it plays
    t = log["tim"]
    assert t[0x2C] == 2000 and t[0x30] == 2 and t[0x00] == 0 and t[0x0C] == 0
    assert not t[0x44] & 0x8000 and t[0x20] == 0
    assert log["rcc"] == AT_START["rcc"] and log["gpioa"] == AT_START["gpioa"]
    assert log["ccr4"][:481] == [1000] * 481 and log["ccr4"][-321:] == [1000] * 321
    x = played(log)
    want = k6_midi.render_table(blob)
    assert len(x) == len(want) and np.array_equal(x, want)
    frames = log["lcd"]["frames"]
    assert [f["text"][2] for f in frames] == ["Test tune", "Again"]
    assert frames[1]["text"][3] == "song 2 of 2"
    assert [on for _, on in log["led"]].count(1) == 16 and log["led"][-1][1] == 0


@need_clang
def test_the_tune_is_in_tune(built):
    blob = songs_table("title: A\ntempo: 60\nlegato: 1\na: A4:2\n")
    x = k6_midi.render_table(blob)[:32000].astype(float) - 1000
    seg = x[4000:28000]
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    f = np.argmax(sp) * 16000 / len(seg)
    assert abs(f - 440) < 1
    assert 180 < np.abs(x[:1000]).max() < 300              # velocity 100: a voice swings ~230 counts
    ret, said, writes, log = emulate(with_songs(built, blob), level=2)
    assert ret == 1 and np.array_equal(played(log), k6_midi.render_table(blob))


@need_clang
def test_default_nokia_tune_matches_the_model(built):
    code, entry = built
    vol, songs = k6_midi.read_table(k6_midi.default_table())
    assert songs[0][0] == "Nokia Tune (Tarrega)"
    s = k6_midi.load(os.path.join(k6_midi.SONG_DIR, "nokia_tune.txt"))
    blob = k6_midi.table([(s.title, *k6_midi.arrange(s))])
    ret, said, writes, log = emulate(with_songs(built, blob), level=4)
    assert ret == 1 and said[-1].endswith("late=0")
    assert np.array_equal(played(log), k6_midi.render_table(blob))


@need_clang
def test_a_press_skips_and_a_hold_stops(built):
    long_song = "title: Long\ntempo: 60\na: C5:4 D5:4\n"
    blob = songs_table(long_song, TEST_TXT)
    # a 100 ms press half a second in skips the long song; the test tune then plays to its end
    ret, said, writes, log = emulate(with_songs(built, blob), keys=((600_000, 700_000),))
    assert ret == 1 and said[-1].startswith("midi done songs=1 ")
    assert said[2] == "midi play 2/2 Test tune"
    # a hold stops it at once, and everything goes back
    ret, said, writes, log = emulate(with_songs(built, blob), keys=((600_000, 2_000_000),))
    assert ret == -2 and said[-1].startswith("midi done songs=0 ") and len(said) == 3
    assert writes[-1] == (0x47, 0x6140) and log["tim"][0x00] == 0
    assert [on for on, _ in log["spk"]] == [True, False] and log["gpioa"] == AT_START["gpioa"]
    # level 2 (no key_poll): key_down every 8 ms works the same
    ret, said, writes, log = emulate(with_songs(built, blob), keys=((600_000, 700_000),), level=2)
    assert ret == 1 and said[2] == "midi play 2/2 Test tune"


@need_clang
def test_refusals(built):
    code = with_songs(built, songs_table(TEST_TXT))
    ret, said, writes, log = emulate(code, tim_busy=True)
    assert ret == -4 and not writes and not log["ccr4"] and not log["spk"]
    ret, said, writes, log = emulate(code, spare=500)
    assert ret == -3 and not writes
