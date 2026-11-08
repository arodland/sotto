# Band surfing on the K6 (surf)

*2026-11-08.* Jeff's idea: a screen that shows signal strength across frequency as a bargraph,
the orange side keys jumping from peak to peak, UP/DOWN stepping along the frequency axis, and the
listening frequency drawn as a blinking bar with a caret above it.

**Status: runs on the radio** (bench run 1, 2026-11-08, below). It is also tested on the emulated M0
against a modelled receiver and the real keypad matrix. It needs API level 6 (the module screen), so
it runs on the image already on the K6 (ffa7dbf, level 7) with no reflash: 3.8 KB of code in
the 4 KB store, receive only.

![four emulated screens](img/k6-surf-screens.png)

*Emulated screens, drawn pixel for pixel as the firmware's `draw_mod_screen` draws them
(`tools/k6_screen.py`): sweeping with the bar on a quiet 440.600; the same a frame later (the bar
blinks); SIDE1 jumped to the -70 dBm carrier on 440.850 and the audio opened (RX); SIDE1 again,
to the next peak at 441.200, a repeater the explorer saved, so line 5 gives its call, offset and
tone. The dots in the top row mark saved repeaters.*

## Using it

Upload `mod-surf.bin` (CI builds it with the other modules) the usual way, for example
`tools/k6_upload.py mod-surf.bin --api-level 6 --key-file ~/.config/sotto/k6-mod.key`. It
starts on the band holding the pager's frequency (70 cm on the bench), centred on it.

| key | does |
|---|---|
| UP / DOWN | the bar one bin up / down; held, it repeats. Past the window's edge the window pans a quarter span |
| SIDE1 / SIDE2 (orange) | next peak above / below that a settled read confirms (10 dB over the settled floor). With none left in the window it pans half a span and looks again after the sweep, on up the band until it finds one or reaches the band's edge ("no peak") |
| BAND or # | next band: 2m (144-148, 10 kHz), 1.25m (222-225, 10 kHz), 70cm (430-450, 25 kHz), GMRS (462.550-467.725, 12.5 kHz), MAR/WX (156.000-162.550, 25 kHz: marine and NOAA weather) |
| 1 / 3 | zoom in / out around the bar: bins of 5, 10, 12.5, 25, 50, 100 kHz (64 bins = 0.32-6.4 MHz on screen) |
| * | monitor: audio open on the bar all the time, no sweeping (title says MON) |
| 0 | hold: no refresh sweeps while a signal plays (title says HOLD) |
| MENU | says the bar's frequency and level on the serial port |
| EXIT | leave; the module waits for the key's release so the pager doesn't see it |

Screen: the title bar says the band and bin step; line 1 the bar's frequency and its level
(a settled read, dBm) and RX while the audio is open; the bargraph (64 bins x 2 pixels, a
tick over each peak, a dot in the top row over each repeater the explorer saved, the caret over
the bar); line 5 the window's edges, or with the bar on a saved repeater its call, offset and TX
tone (`N0CALL +5.0 103.5`), or "no peak" for 1.5 s.

## How it works

**One receiver, so it listens first.** While the bar's channel is quiet the module sweeps with
the audio muted. Every 16 bins it goes back to the bar for a settled read (12 ms after the
tune). A signal there 10 dB over the settled floor opens the audio (REG_47 FM and the speaker
amplifier on PB2, as `dmrdet`), and the module parks on it. It closes 1.5 s after the signal
drops and sweeps again. While parked, one refresh sweep every 5 s (about 0.3 s of muted audio)
keeps the bars current, unless hold is on.

**Levels.** Sweep reads are quick: 3 ms after the tune, like Aura's own spectrum. That is
about 59% of the way in dB from the -173 dBm restart (explorer Phase 0), so the bars are
relative. The floor is the quartile of the window's bins, one pixel is about 1.1 quick dB, and
a bar rises at once but falls at most 3 dB a sweep. A peak is a local maximum 6 quick dB over
the floor. The squelch and line 1 use settled reads against a settled floor: the second lowest
of 8 bins' settled reads, measured when the window changes and every 30 s of sweeping. A
quieter read of the bar lowers it. The AGC is left alone, because freezing the gain, as `dmrdet`
does, shifts RSSI by up to 15 dB.

**Speed (estimated from the emulator and Phase 0's ~0.9 ms tune).** A bin costs about 3.9 ms,
so a 64-bin sweep plus four checks of the bar takes about 0.3 s: about 3 sweeps a second while
nothing plays.

**Keys without a key API.** The module API has `key_down` but no key identity, so the module
scans Aura's matrix itself. The rows are PB0/PB4/PB10/PB11/PB9 and the columns
PC13/PC14/PF6/PB14; the side keys read with no row driven (Aura `src/device/keypad.rs` at
edf3b98). It writes only the row bits' BSRR/BRR, as `key_poll` does, and leaves the rows idle.

**Saved repeaters.** At the start the module reads the explorer's repeater table (c/k6/rt.h,
0x0F0000, level 5's `fl_read`, 8 bytes a slot, about 0.1 s) and keeps the frequency and slot of
up to 24 valid records. With the bar on one it reads that record's call, offset and TX tone for
line 5. It never writes the flash. This is the first piece of the VFO idea below: the bargraph
knows which peaks are repeaters, and PTT could later use the offset and tone it shows.

**Memory.** The 480 B bitmap and the ~400 B state are on the stack (`-fstack-usage`: about
1.2 KB deepest of the 3.5 KB a module has at its call). The store holds only the code, so nothing
changes when PR #142 grows the store.

## Bench build (surfauto)

`mod-surfauto.bin` is the same module for unattended runs: SIDE1 presses itself every 4 s, saying
`sel <MHz> <dBm>` first. It wraps to the band's bottom at the top and leaves after 60 s. The keys
still work. Before it ends it says its last bargraph: `bar <first bin MHz> <step>`, then two
lines of 32 letters, one a bin, `a` for 1 pixel up to `x` for 24 (about 112 bytes of the 684 the
telemetry carries). `python tools/k6_screen.py RUN.txt OUT.png` draws it as the screen showed
it, so a run's spectrum comes back with no serial log. With `--telemetry 12` the K6 sends those lines back over the air, so the bench needs
no one at the radio and no serial cable:

```
tools/k6_upload.py mod-surfauto.bin --api-level 6 --telemetry 12 --run-s 70 --key-file ~/.config/sotto/k6-mod.key
```

A run on the bench should list the active 70 cm repeaters (441.000, 441.100, 441.200, 440.925
DMR when up). For a known peak, the IC-7100 can send PSK31 `AG7EW TEST` on a free 70 cm
frequency during the run (`modem/psk31.py`), back on 440.850 before the module ends so the
report gets through. In the emulator (tests/test_k6_surf.py) it finds 440.850, then 441.200,
wraps from 450 to 430, then finds 430.300 and the others again.

## Bench run 1 (2026-11-08 08:08Z, hamlet, receive only)

`mod-surfauto.bin` (CI 5877cc6) went up by `tools/k6_upload.py` on 440.850. All 30 XL chunks
arrived on the first send. It ran 60,053 ms, returned 5 (sweeps), and had 2,128 B of stack
free. Its 13 `sel` lines came back over the air:

```
surf 70cm 440.850 25k
sel 440.8500 -35      the IC-7100 still keyed after the upload
sel 440.9250 -103     sel 440.9750 -103     sel 441.0750 -110     sel 441.1250 -108
sel 441.5500 -103     sel 441.6250 -107     sel 441.8000 -118     sel 441.8500 -122
sel 441.9250 -104     sel 442.2000 -104     sel 442.3250 -104     sel 442.4250 -103
end sw=5
```

What it showed:
- The module, the screen calls and the keypad scan run on the real image, and the stack has
  room to spare.
- **The side keys stopped on noise.** Each peak was a bump in the quick reads that a settled read
  put at only -103 to -122 dBm, so the climb took 13 presses to reach 442.4 and never wrapped.
  Each of those bumps opened the squelch, so the module spent most of the run parked, which is why
  it made only 5 sweeps. Fix (the PR after #143): a candidate peak counts only when a settled read
  puts it 10 dB over the settled floor (12 ms a candidate). The emulator now has bumps that show
  only in quick reads (`test_side_keys_skip_bumps_a_settled_read_doesnt_confirm`), and the old
  code fails that test.
- No serial log: auto mode refused the read-only logger, so the run's bargraph itself isn't
  recorded. Next run: allow `k6_harness listen` on ttyUSB2 (read-only) or watch the screen.

## The bargraph as the radio's main VFO? (Jeff's thought, 2026-11-08)

It fits, and this module is the way to try it. Points for and against:

- **Listening comes first, which a VFO must do.** The module never sweeps while a signal plays
  on the bar, except for the refresh, which hold turns off. So as a VFO it behaves like a normal
  receiver with a live spectrum whenever the channel is quiet. A single-receiver radio can't
  do better. The cost is that the bars go stale during a long QSO.
- **Fit with Aura.** The pager-only and EXPLORE images already drop Aura's standby screen and
  menus, so the surf screen could be their home screen, with no change to stock Aura. As a flash
  feature it is about 3 KB of C, and the EXPLORE image has about 14 KB of flash to spare (108.9
  of 122.9 KB). It draws straight into Aura's display buffer, so the bitmap goes away. RAM is the
  64-byte bar array and about 200 B of state.
- **What a VFO adds:** PTT on the bar, which the module API can't do. TX belongs in the core
  with its band lock. The repeater table (c/k6/rt.h) gives offset and tone, so PTT on a known
  repeater's output keys its input. Frequency entry by digits and a squelch setting are the
  other additions.
- **Suggested path:** run `surf` on the radio first, so Jeff can judge the feel (sweep rate,
  blink, how stale the bars get while listening). If he likes it, move the C core into the
  EXPLORE image as its home screen, with EXPLORE's candidate list one key away and PTT through
  the repeater table. Combined with the planned page-buffer screen, it can replace the EXPLORE
  list screen.

## Files

- `c/k6/mod/surf.c`: the module; `c/k6/mod/surfauto.c`: the bench build.
- `tests/test_k6_surf.py`: the emulated runs (bargraph, side keys, UP/DOWN and panning, band
  and zoom, monitor, squelch hang, EXIT, saved repeaters from a table in the emulated flash, the
  bench build and its bargraph lines). `python tests/test_k6_surf.py OUT.png`
  redraws the screens above.
- `tools/k6_screen.py`: draws any level-6 module's emulated frames as the LCD shows them, and
  (run as a script) the bargraph a surfauto run said.
- `tests/test_k6_i2s.py`: `run(..., keypad=[(from_us, to_us, key)])` drives the real key matrix
  for modules that scan it.
