# The K6 MIDI player module

`c/k6/mod/midi.c` plays songs out of the K6's speaker from RAM, with no reflash. It is a receive
side module: it never keys the transmitter, so it needs no ident and no band rules. Jeff asked for
it on 2026-11-09 ("just for fun"), then for the classic Nokia ring tones.

## What it sounds like

Four voices are mixed at 16 kHz. Each voice reads a 256-sample wavetable (harmonics 1 to 4 at
19000, 7500, 4000 and 2000 in Q15, built in the scratch RAM when the module starts) and has a
plucked envelope: a 4 ms attack to the note's velocity, a fall to half in about 20 ms, a slow
fade (time constant about 1 s) while the note is held, and a release of about 30 ms when it
ends. A new note takes a free voice, or else the quietest one; the voice keeps its phase, so a
stolen voice doesn't click.

The audio path is the one `spksweep` measured (docs/k6-receiver.md, "The audio path"): PA11 is
TIM1_CH4 with a 48 kHz PWM carrier (ARR 2000 at 96 MHz), an update every 3 carrier periods, and
the module writes CCR4 itself on each update event. PB2 turns the speaker amplifier on. The
FD6818's AF output (REG_47) is muted while it plays and put back afterwards, along with TIM1,
PA11's mode and the amplifier. One voice at velocity 100 swings about 230 counts either side of
1000 (spksweep's tones were 250); the mix is clipped at 16 and 1984.

Previews of every score, sample for sample what the module writes (before the RC filter and the
speaker), are in the project folder `k6-midi/` (made with `k6_midi.py wav`).

## Keys, screen, LED

- A key press skips to the next song; holding a key for a second stops the player.
- With level 4 images it checks `key_poll` every millisecond; on older images `key_down` every
  8 ms (about 60 us, so one late sample in eight blocks).
- On level 6 images the screen shows the song's title and "song 2 of 6" between songs. It never
  draws while a song plays: a flush holds the CPU for 20-40 ms, which would be a gap in the sound.
- On level 7 images the green LED blinks on each note.

Serial lines: `midi songs=6 bytes=1539 vol=100`, `midi play 1/6 Nokia Tune (Tarrega)` for each
song, `midi done songs=6 notes=405 late=0`. `late` counts samples that missed their update
event (an interrupt held the CPU over 62 us; the sample before played twice). Returns the songs
played to the end, -2 (a held key stopped it), -3 (scratch under 512 bytes), -4 (TIM1 already
running), -5 (no song table).

## The songs

CI's `mod-midi.bin` (K6 pager firmware artifact) carries `c/k6/mod/midi_songs.h`, generated from
the text scores in `tools/midi_songs/` by `python3 tools/k6_midi.py header`. They are our own
transcriptions of public-domain tunes. First the old Nokia ring tones that are public domain:

| Song | Source |
| --- | --- |
| Nokia Tune (Tarrega) | Francisco Tarrega, Gran Vals (1902), bars 13-16, twice |
| Fur Elise | Beethoven, WoO 59 (1810), the A section twice |
| Mozart 40 | Mozart, Symphony No. 40, K. 550 (1788), the opening theme |
| Bumblebee | Rimsky-Korsakov, Flight of the Bumblebee (1900), the opening run |
| Valkyrie | Wagner, Ride of the Valkyries (1856), the horn call |
| Ode to Joy | Beethoven, Symphony No. 9 (1824) |

Nokia's own compositions (Ring Ring, Hurdy Gurdy and the rest) are under copyright and aren't
here. The other scores in `tools/midi_songs/` (Greensleeves, Minuet in G, Twinkle Twinkle) are
left out of the default build so that it fits the EXPLORE image too; `patch` puts them in.

**Fit.** The module is about 2.5 KB of code plus the table (1.5 KB by default: 4,011 bytes in
all in CI, 2,472 of them code; clang here makes it a little bigger). It needs 512 bytes of scratch for the wavetable, and an upload with `--telemetry` keeps 696
bytes at the top of the block, so the default build fits the EXPLORE and lean images' 5,376-byte
block. The SPI voice images (pager-only, lr, vlr, vlrh) take about 8 KB of songs.

## Other songs

```sh
python3 tools/k6_midi.py info song.mid                 # notes, length, bytes
python3 tools/k6_midi.py patch mod-midi.bin my-midi.bin song.mid tools/midi_songs/greensleeves.txt
python3 tools/k6_midi.py wav preview.wav song.mid      # what the K6 will play
python3 tools/k6_upload.py my-midi.bin --api-level 2 --telemetry 3 --run-s 120 --key-file ~/.config/sotto/k6-mod.key
```

`patch` needs no compiler (hamlet has none): the table is the last thing in the module's code,
so it cuts the code at the table's magic (`K6MIDI`, format 1) and appends the new table. The
module reads the table through an empty `asm` so the compiler can't fold the built-in one in.

What the tool does to a MIDI file (format 0 or 1, through its tempo map): leaves out channel 10
(the drums; `--drums` keeps it), folds notes into MIDI 40-95 by octaves (E2 to B6: the speaker
does little below and the harmonics alias above; `--low`, `--high`), keeps at most four notes
sounding (a new note that is the new top or bottom cuts an inner one short, else it is left
out), rounds velocities to steps of 8, and puts every time on a grid: 1/24 of a quarter note for
a file with one tempo, 8 ms otherwise. `--transpose`, `--max-s` and `--volume` do what they say.

Use public-domain music: a MIDI file someone sequenced can be a work of its own even when the
tune is old.

The text score format (one item per line, # starts a comment line):

```
title: Ode to Joy
tempo: 116
mel: F#5 F#5 G5 A5 | A5 G5 F#5 E5 | D5 D5 E5 F#5 | F#5:1.5 E5:0.5 E5:2
bass: !80 D3:2 A3 | G3 A3 | D3 F#3 | A3 A3
```

Every voice starts at time 0. A token is a note (C4 is middle C) with an optional `:len` in
quarter notes (it carries on until changed) and `~ring` (sound that long while the voice moves
on: a pedal), a chord `C4+E4+G4`, a rest `r`, or `!vel`. `legato:` (default 0.9) is how much of
its length a note sounds.

## Song table format

Little-endian, read a byte at a time (the M0 faults on unaligned loads):

| Offset | Field |
| --- | --- |
| 0 | `K6MIDI`, format 1, reserved 0 |
| 8 | u16 bytes of the whole table |
| 10 | u8 songs, u8 volume (a voice at full velocity swings volume / 8192 x 32767 counts) |
| 12 | u16 offset of each song from the table's start |

A song is a u16 unit (samples at 16 kHz per time unit), its title (NUL-terminated, up to 21
characters), then events: 0x00-0x7F note on with that MIDI number, followed by its length in
units as a MIDI-style variable-length number (up to 3 bytes); 0x80-0xBF wait (b & 0x3F) + 1
units; 0xC0 wait a variable-length number of units; 0xC1 v the velocity of the notes that follow;
0xFF the end. Events run once a millisecond (16 samples), so a note starts within 1 ms of its
time. About 3 bytes a note.

## Tests

`tests/test_k6_midi.py` (CI): the module on the emulated M0 against a modelled TIM1 must write
exactly the PWM values `k6_midi.render_table` computes, for a test tune (a five-note chord the
tool trims to four) and for the default Nokia Tune; a press skips, a hold stops, level 2 polls
`key_down`; TIM1 busy and a small scratch are refused; the scores go through a standard MIDI
file and come back the same; the header is up to date. The worst stretch between two samples in
the emulator is about 1,300 instructions (under 25 us at 96 MHz, against 62.5 us).

## Bench

**On the K6, 2026-11-09 06:22-06:25Z** (image cf9516e aura-pager-vlrh, CI's `mod-midi.bin` of
0681507, 4,011 B, uploaded with `k6_upload.py ... --api-level 2 --telemetry 2 --run-s 110`): all
34 XL chunks arrived on the first send (44 s on air); the K6 ran it and returned 6 (all six
songs), `stack_free` 1,812 B, `midi done songs=6 notes=405 late=6`: six samples of about 1.5
million missed their 62.5 us slot. The report said it ran 4,779 ms: the module called `ticks()`
once a second to keep the core's count, but the core sums the run time only in `ms()` and
`delay_us()`, so 96 s wrapped. It calls `ms()` now. Two telemetry parts held only the last say
lines; use `--telemetry 3`. Nothing on hamlet hears the K6's speaker, so there is no recording.
