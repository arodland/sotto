# GROUNDWAVE: an artillery game for the K6

GROUNDWAVE (2026-11-09, Jeff's idea) is a turn-based artillery game in the classic genre:
two stations on a hilly field take turns lobbing shots at each other, with wind, craters and a
shop between rounds. Its look, terrain, field and weapons are its own, and every item is a
radio joke. It runs as a RAM module (`c/k6/mod/gw.c`, level 6, the LED at 7) and can be played
two ways:

- **Hot seat**: one radio, passed between two players.
- **Radio**: two K6s, each running the module, on 440.850 MHz. The radios swap moves over
  the FD6818's own FSK modem, and each one works out the flight itself.

| File | What it is |
|---|---|
| `c/k6/mod/gw_game.h` | The rules: terrain, flight, weapons, shields, money. Pure C with integer arithmetic only. |
| `c/k6/mod/gw.c` | The module: keys, screens and play over the air. |
| `c/k6/mod/gw_auto.c` | Bot build: the radio plays a hot-seat game against itself (bench, tests). |
| `c/k6/mod/gw_call.c` | Bot build that calls over the radio. |
| `c/k6/mod/gw_answer.c` | Bot build that answers a call. |
| `c/k6/gw_host.c` | The same rules built for the PC, for the tests and a PC opponent. |
| `modem/k6gw.py` | Packets, the rules through ctypes, and `Peer`, a PC opponent that follows the protocol. |
| `tests/test_k6_gw.py` | Host rules, hot seat, and radio play against `Peer` over a modelled FSK modem. |

## The field and the screen

The field is the level-6 bitmap: 128 x 30 pixels, between text lines 0-1 and 5. Line 0 shows
both stations' health and the round, and a `>` marks whose turn it is. Line 1 shows the angle,
the power and the wind. Line 5 shows the item in hand and how many are left, or a message.

Each round makes a new field: midpoint-displacement hills, with the two stations on flat pads,
P1 on the left third and P2 on the right. A shack with a ground-plane mast is P1, an open shack
under a dipole is P2. A station that is knocked off the air becomes a heap of rubble.

Flights are drawn as they happen, with a dotted trail, a 2 x 2 shot, and a caret on the top row
while a shot is above the screen. A blast shows a disc and then a crater, and the dirt above
the crater falls in. A station whose ground goes falls with it and takes damage (2 points per
pixel past the first).

## Keys

The module scans the keypad matrix itself, as surf.c does.

| Keys | Action |
|---|---|
| 4 6, or the side keys | Angle (hold to repeat) |
| 1 3 | Angle, 10 at a time |
| 2 8, or UP DOWN | Power |
| 7 9 | Power, 10 at a time |
| * # | Previous or next item held |
| MENU or 5 | Fire |
| EXIT twice | Leave the game (from the title screen, leave the module) |
| Shop: UP DOWN (2 8) | Pick an item |
| Shop: MENU (5) | Buy |
| Shop: EXIT | Done |

Title screen: 1 hot seat, 2 radio, 3 help, EXIT quit.

## Weapons and shields

Everyone starts with $80 and unlimited QRP Shots.

- **Pay per round**: $30, plus the damage you dealt, plus $40 for winning it.
- **Power**: 0 to 100. Wind changes from turn to turn, by up to 9 either way.

| Item | $ | Qty | What it does |
|---|---|---|---|
| QRP Shot | free | ∞ | 5 W: a small blast (radius 3) |
| Kilowatt | 60 | 1 | QRO: a big blast (radius 8, 80 damage) |
| FCC Violator | 35 | 2 | Splatter: the widest blast (radius 11), but its power stops at 55, so it can't reach far |
| 12V Charger | 45 | 2 | Throws sparks that land around it and blast again (the funky bomb) |
| CW Strike | 30 | 3 | Ignores the wind, and a direct hit does 100 damage, but the blast is tiny (radius 2) |
| Beverage | 40 | 2 | A strong, wide blast (65 damage), but it can't rise above a ceiling (the dotted row while aiming) |
| The Lid | 15 | 3 | Wanders off its path, a different way each time |
| Skip Zone | 30 | 3 | Skips off the ground before it blasts |
| Repeater | 55 | 1 | Kerchunk: splits into three at the top of its arc |
| Yagi Beam | 35 | 2 | A straight line, with no gravity and no wind; it fizzles after 70 steps |
| Coax Spool | 20 | 2 | Does no damage, but piles up a mound of dirt (a wall, or a burial) |
| Tropo Duct | 25 | 3 | Half gravity: it flies far |
| Ground Rod | 30 | shield | Soaks up half of each hit until its 60 points are gone, and nudges shots a little |
| Faraday Cage | 70 | shield | Bends shots away (radius 12) and soaks up all of a hit until its 100 points are gone |

Shields work like the classic magnetic shield. A shot inside the shield's radius is pushed away
from the station (but not its owner's own shot), and a blast's damage comes off the shield's
energy first. A station can hold one shield at a time. The dome is drawn dotted while it has
energy.

A game is best of 3 rounds (`GW_ROUNDS`). If both stations go off the air together, the round
is a draw.

## Radio play

In radio play, one radio calls (MENU in the lobby) and the other answers (MENU when it shows
"AG7EW calls!"). The caller picks the seed, which fixes every field and wind of the game.

1. Both stations shop, and swap their shops in SHOP packets.
2. Both build the round's field.
3. In turn, the station to move sends a MOVE (angle, power, item, and its game hash), and both
   radios fly the same shot.

Every packet except an ACK is acknowledged. The sender tries 4 times, with a random pause
between tries. A repeated packet (same seq) gets its ACK again and is otherwise dropped. If a
MOVE's hash doesn't match the receiver's, line 5 shows DESYNC, and the move is still played.

Each over follows the same steps:

1. Wait for a clear channel: below -90 dBm for 500 ms, giving up after 15 s.
2. Key up with `tx_on`, which works only on the pager's frequency at low power.
3. After a 300 ms lead, send the packet twice on the pager's PHY F (FFSK 1200/1800 Hz at 1800
   baud, the registers of aura-pager.patch).
4. Send AG7EW in MCW (800 Hz, 25 wpm) on the tone oscillator.
5. Key down and listen again.

That is about 3.7 s keyed, plus the time `tx_on` takes. A key pressed during an over keys down
at once. The module refuses radio play when the pager isn't on 440.850 ("radio: 440.850 only").

Packets are 64 bytes, PN9-whitened as the pager's (magic 0xA6, which no other packet uses):

```
0 0xA6   1 type (1 HELLO 2 JOIN 3 SHOP 4 MOVE 5 ACK 6 BYE)   2-3 game id (LE)   4 seq
5 from (station 0/1)   6 round   7 turn   8-11 sender's gw_hash before its action (LE; 0: no check)
12..  HELLO: seed (4, LE), rounds | SHOP: shield, inv[14], money (2, LE) |
      MOVE: angle, power, item | ACK: the seq and type it answers
46-51 "AG7EW"   62-63 CRC-16/CCITT-FALSE of bytes 0-61, big-endian
```

The answerer plays the caller's number of rounds (HELLO byte 16). The rules are integer-only,
and the bot never touches the game's random generator, so the two radios stay in step to the
bit. `modem/k6gw.py`'s `Game` is the same C file built for the PC, so a PC opponent stays in
step too. `test_radio_game_against_the_pc` checks the hashes every turn.

## Memory

| Build | Code (clang 18, -Oz) |
|---|---|
| gw | 11,657 B |
| gw_auto | 11,279 B |
| gw_call | 11,147 B |
| gw_answer | 11,211 B |

The block holds 11,752 B of code. The state is about 1.2 KB: the game (392 B), the 480-byte
bitmap, and three packet buffers. The trail dots share the receive and transmit buffers, since
nothing is sent or received while a shot flies.

The code leaves less scratch than the state needs, so `k6_main` puts the state on the stack.
The emulator measures 1.8 KB of stack at the deepest point. The voice SPI images give a module
about 3.1 KB (the vlrh image's 3,112 B, docs/k6-modules.md), which leaves about 1.3 KB for
Aura's own calls. The tests fail above 2,000 B.

The game needs a voice SPI image (its 11,776-byte block). The EXPLORE and lean images' 5,376 B
are too small.

## Tests

`python -m pytest -q tests/test_k6_gw.py` (about 30 s with `-n 4`) covers:

- **The rules on the PC**: the same seed and moves give the same game, and each weapon does its
  trick (the Beverage stays under its ceiling, the FCC Violator flies short, the Yagi flies
  straight, the Lid wanders, the Coax piles dirt). It also checks shields, money, hits and the
  end of a game.
- **Hot seat**: the bot plays a whole game on the screen and never transmits. A person's keys
  can shop, aim and fire.
- **Radio**: `gw_call` and `gw_answer` each play a whole game against `k6gw.Peer`, over a model
  of the FD6818's FSK modem. The model covers REG_59 control, the 8-word FIFO and its interrupts,
  the RSSI, and the other radio's overs. The runs are played with one copy lost, and with a
  whole over lost each way, so both sides have to repeat. The hashes stay in step.
- **The TX rules** (test_k6_txrules.py's, here with the modem model): `tx_on` only on 440.850,
  never on a busy channel, only FSK and tone registers written while keyed, every over ending
  in AG7EW, and a key ending an over with the modem quiet.

## On the bench

Not on air yet. Use a RAM module first (no flash): the image must be a voice SPI image (vlrh or
similar) at level 6 or above.

1. **Hot seat, no TX**: `mod-gw_auto.bin` plays itself and returns. Check the screen, the
   `gw shot` and `gw end` lines, and `stack_free=` in `K6 MOD ret` (it should be over 512 B).
2. **Hot seat by hand**: `mod-gw.bin`, key 1, play a round.
3. **A few overs on the air**: `mod-gw_call.bin` on 440.850 calls with up to 4 HELLO overs,
   about 12-14 s apart, and quits when nothing answers (EXIT stops it sooner). Record them on the IC-7100 (receive only): two FSK packets and
   then AG7EW in MCW each time. This checks that chip FSK goes out with `tx_on`'s TX state (the
   core's ARQ sends it from Aura's own TX setup).
4. **Two radios**: a second K6, or a PC peer on the IC-7100 (`modem/k6gw.py`'s `Peer` with the
   FSK modem of modem/fsk.py; not written yet), once the IC-7100 has an antenna back.
