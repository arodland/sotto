"""GROUNDWAVE (c/k6/mod/gw.c, the rules in c/k6/mod/gw_game.h) on an emulated Cortex-M0.

  - the rules on the host (c/k6/gw_host.c): terrain, flights, the weapons' tricks, damage,
    shields, money, and that the same seed and moves give the same game;
  - hot seat: the bot build (gw_auto.c) plays a whole game on the screen, and a person's keys
    (shop, aim, fire) work in gw.c;
  - radio play: the bot builds (gw_call.c, gw_answer.c) play a whole game over a modelled
    FD6818 FSK modem against a PC opponent (modem/k6gw.py Peer, the same rules built for the
    host): packets, ACKs, repeats after a lost packet, both sides' game hashes in step;
  - the transmit rules: 440.850 only, never on a busy channel, tx_on asked for little, only the
    FSK and tone registers written while keyed, every over ends in AG7EW in Morse, and a key
    ends an over.
SPDX-License-Identifier: MIT"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, run  # noqa: E402
from test_k6_txrules import morse  # noqa: E402

from modem import k6gw  # noqa: E402

MOD = os.path.join(ROOT, "c", "k6", "mod")
BLOCK = 11752                                  # the module block less its header and tag
RF_HZ = 440_850_000
PKT_US = (16 + 4 + 64) * 8 * 1_000_000 // 1800     # preamble, sync, payload at 1800 baud
WORD_US = 16 * 1_000_000 // 1800
TX_ON_US = 1_500_000                           # tx_on's key-up on the radio
STACK = 2000          # the state is on the stack (the code leaves too little scratch); the voice
                      # images give a module about 3.1 KB, so this leaves 1.1 KB for Aura's calls
FSK_REGS = {0x02, 0x3F, 0x58, 0x59, 0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F, 0x70, 0x71, 0x72}


def build(name, **defs):
    from modem import k6mod
    src = os.path.join(MOD, name + ".c")
    if not defs:
        return k6mod.build(src)
    with tempfile.TemporaryDirectory() as d:
        w = os.path.join(d, "w.c")
        with open(w, "w") as f:                 # (the build reads level and -Oz from this file)
            f.write(f"#define K6_MODULE_OPT {k6mod.source_opt(src)}\n")
            f.writelines(f"#define {k} {v}\n" for k, v in defs.items())
            f.write(f'#include "{src}"\n')
        return k6mod.build(w)


def rssi_reg(dbm):
    return (dbm + 174) << 1


class Air:
    """The FD6818's FSK modem as the module drives it (the pager's registers: REG_59 control,
    REG_5F FIFO, REG_02/0C interrupts, REG_3F their enables), one channel, and a PC opponent on
    it (k6gw.Peer). The K6's packets reach the peer as it sends them; the peer's overs (two
    copies of a packet, then its CW ID) go out when the K6 is not keyed, and the K6's chip
    receives a copy only if it is armed when the copy starts, its FIFO overflowing past 8
    unread words (the words then come out wrong). lose: the peer's copies to drop, by count."""

    def __init__(self, lose=(), busy=None, peer_aim=None, k6_lose=()):
        self.irq = self.latch = self.en = 0
        self.ctrl = 0
        self.keyed = False
        self.t_unkey = 0
        self.fifo_tx, self.tx_done = [], None
        self.k6_pkts = []                       # (us, decoded) of every packet the K6 sent
        self.queue, self.over = [], None        # the peer's packets to send; its over on air
        self.acks = []                          # the peer's ACKs to send
        self.rx = None                          # a copy the K6's chip is taking in
        self.pending_ack = None                 # the peer's reliable packet, its tries
        self.copies = 0
        self.lose = set(lose)
        self.k6_lose, self.k6_n = set(k6_lose), 0   # the K6's overs the peer misses, by count
        self.busy = busy or []
        self.peer = k6gw.Peer(self._peer_send, aim=peer_aim)
        self.keys = []                          # (name, us)
        self.keyed_writes = []
        self.peer_overs = []                    # (start, end) of every over the peer sent

    # -- the peer's side --
    def _peer_send(self, pkt, reliable):
        (self.queue if reliable else self.acks).append([pkt, reliable, 0])

    def _start_over(self, us):
        item = (self.acks or self.queue)[0]      # (an ACK goes first, as the radio's would)
        pkt, reliable, tries = item
        item[2] += 1
        copies = []
        for k in range(2):
            n = self.copies
            self.copies += 1
            copies.append((us + 300_000 + k * 450_000, pkt, n in self.lose))
        self.over = {"t0": us, "end": us + 3_600_000, "copies": copies, "item": item}
        self.peer_overs.append((us, us + 3_600_000))
        if not reliable:
            self.acks.pop(0)
        else:
            self.pending_ack = (k6gw.parse(pkt)["seq"], us)

    def _advance(self, us):
        if self.over and us >= self.over["end"]:
            self.over = None
        if self.pending_ack and self.queue and self.queue[0][1]:
            seq, t = self.pending_ack
            if any(d["type"] == k6gw.ACK and d["body"][0] == seq and tt > t for tt, d in self.k6_pkts):
                self.queue.pop(0)
                self.pending_ack = None
            elif us - t > 14_000_000 and not self.over:
                self.pending_ack = None          # no ACK: send it again
                if self.queue[0][2] >= 8:
                    self.queue.pop(0)
        if (not self.over and not self.keyed and us - self.t_unkey > 1_500_000
                and (self.acks or self.queue and not self.pending_ack)):
            self._start_over(us)
        # the K6's chip: a copy starting while it listens
        if self.over and self.rx is None and self.ctrl & 0x1000 and not self.keyed:
            for t, pkt, lost in self.over["copies"]:
                sync = t + (20 * 8 * 1_000_000) // 1800
                if sync <= us < sync + WORD_US and not lost:
                    self.rx = {"sync": sync, "pkt": pkt, "read": 0, "full_at": 3, "done": False}
                    self._irq(0x0002)
        if self.rx:
            r = self.rx
            avail = min(32, (us - r["sync"]) // WORD_US)
            if avail - r["read"] > 8:
                r["over"] = True
            if avail - r["read"] >= 3 and avail >= r["full_at"]:
                r["full_at"] = r["read"] + 6
                self._irq(0x1000)
            if avail == 32 and not r["done"]:
                r["done"] = True
                self._irq(0x2000)
        if self.tx_done and us >= self.tx_done:
            self.tx_done = None
            self._irq(0x8000)

    def _irq(self, bit):
        if self.en & bit:
            self.irq |= bit

    # -- the module's side (test_k6_i2s.run's fsk hook) --
    def read(self, reg, us):
        self._advance(us)
        if reg == 0x0C:
            return 1 if self.irq else 0
        if reg == 0x02:
            return self.latch
        if reg == 0x5F:
            r = self.rx
            if r is None:
                return 0
            i = r["read"]
            avail = min(32, (us - r["sync"]) // WORD_US)
            r["read"] = min(i + 1, 32)
            if i >= avail or r.get("over"):
                return 0xA5A5                    # an empty or overrun FIFO: wrong words
            b = r["pkt"]
            return b[2 * i] | b[2 * i + 1] << 8
        if reg == 0x67:
            on = (self.over and self.over["t0"] <= us < self.over["end"]) or \
                any(a <= us < b for a, b in self.busy)
            return rssi_reg(-40 if on else -121)
        return None

    def write(self, reg, v, us):
        self._advance(us)
        if self.keyed:
            self.keyed_writes.append((reg, v, us))
        if reg == 0x02:
            self.latch, self.irq = self.irq, 0
        elif reg == 0x3F:
            self.en = v
        elif reg == 0x5F:
            self.fifo_tx.append(v)
        elif reg == 0x59:
            if v & 0x4000:
                self.rx = None
            if v & 0x8000:
                self.fifo_tx = []
            if v & 0x0800 and self.keyed:
                b = b"".join(bytes([w & 0xFF, w >> 8]) for w in self.fifo_tx[:32])
                self.tx_done = us + PKT_US
                d = k6gw.parse(b)
                self.k6_pkts.append((us, d))
                if d and not (self.k6_pkts[-2:-1] and self.k6_pkts[-2][1] == d):
                    self.k6_n += 1               # (the second copy of an over: a repeat)
                    if self.k6_n - 1 not in self.k6_lose:
                        self.peer.hear(b)
            self.ctrl = v

    def key(self, name, us):
        self.keys.append((name, us))
        self.keyed = name == "tx_on"
        if not self.keyed:
            self.t_unkey = us
        return TX_ON_US if name == "tx_on" else 0


def play(name, air, count=3_000_000_000, keypad=None, rx_hz=RF_HZ, level=7, **defs):
    code, entry = build(name, **defs)
    lcd, tx = {}, {}
    ret, said, regs, writes = run(START, code=(code, entry), level=level, lcd=lcd, keypad=keypad or [],
                                  spare=BLOCK - len(code), count=count, rx_hz=rx_hz, tx=tx, fsk=air,
                                  paint=True)
    return ret, said, regs, tx, lcd


def overs(tx):
    """[(t_on, t_off)] of every key-up"""
    log = [(n, us) for n, _, us in tx.get("log", []) if n in ("tx_on", "tx_off")]
    return [(a[1], b[1]) for a, b in zip(log[::2], log[1::2])]


# ---- the rules, on the host ----

def test_rules_layout_and_determinism():
    a, b = k6gw.Game(1234), k6gw.Game(1234)
    for g in (a, b):
        g.new_round()
    s = a.state()
    assert s.round == 1 and 8 <= s.p[0].x <= 35 and 92 <= s.p[1].x <= 119
    assert s.p[0].hp == s.p[1].hp == 100 and s.p[0].money == 80
    assert all(9 <= s.top[x] <= 29 for x in range(128))
    for move in [(45, 70, 0), (135, 65, 0), (50, 80, 0), (130, 72, 0)]:
        assert a.hash() == b.hash()
        a.shot(*move)
        b.shot(*move)
    assert a.hash() == b.hash() and a.state().turn == 4
    c = k6gw.Game(1235)
    c.new_round()
    assert c.hash() != k6gw.Game(1234).hash()


def trace(g, a, pw, w):
    import ctypes
    tr, n = (ctypes.c_int16 * 4000)(), ctypes.c_int()
    g.L.gw_h_shot(g.g, a, pw, w, tr, 2000, ctypes.byref(n))
    return [(tr[2 * i], tr[2 * i + 1]) for i in range(n.value)]


def give(g, j, item, n=5):
    g.L.gw_h_give(g.g, j, item, n)


def test_weapons_tricks():
    def fresh():
        g = k6gw.Game(99)
        g.new_round()
        for i in range(1, 14):
            give(g, 0, i)
        return g
    # Beverage never climbs above its ceiling (row 8) once it is below it
    pts = trace(fresh(), 60, 80, 5)
    assert min(y for x, y in pts[3:]) >= 8
    # QRP at the same aim goes higher
    assert min(y for x, y in trace(fresh(), 60, 80, 0)) < 8
    # FCC Violator: full power flies no further than QRP Shot at 55
    def far(pw, w):
        t = trace(fresh(), 45, pw, w)
        return max(abs(x - t[0][0]) for x, y in t)
    assert far(100, 2) <= far(55, 0) < far(100, 0)
    # Yagi Beam: a straight line
    pts = trace(fresh(), 20, 50, 9)
    (x0, y0), (x1, y1) = pts[0], pts[min(20, len(pts) - 1)]
    mid = pts[min(10, len(pts) - 1)]
    assert abs((mid[1] - y0) * 2 - (y1 - y0)) <= 2
    # The Lid wanders off QRP's path
    a, b = trace(fresh(), 45, 70, 6), trace(fresh(), 45, 70, 0)
    assert max(abs(p[1] - q[1]) for p, q in zip(a, b)) >= 2
    # Coax Spool piles dirt up, a crater digs it out
    g = fresh()
    before = sum(g.state().top[:128])
    g.shot(45, 70, 10)
    assert sum(g.state().top[:128]) < before
    g = fresh()
    g.shot(45, 70, 1)
    assert sum(g.state().top[:128]) > before
    # an item not held becomes QRP Shot, and firing uses one up
    g = fresh()
    g.shot(45, 70, 1)
    assert g.state().p[0].inv[1] == 4


def test_shields_and_money():
    g = k6gw.Game(7)
    g.new_round()
    g2 = k6gw.Game(7)                            # $80 to start: a $70 Faraday Cage, and one shield only
    assert g2.buy(0, 13) == 0 and g2.buy(0, 12) == -1 and g2.state().p[0].money == 10
    assert g2.buy(0, 1) == -1                    # $60: no money left
    g2.new_round()
    s = g2.state()
    assert s.p[0].sh == 13 and s.p[0].she == 100
    del s


def test_hits_end_rounds_and_pay():
    g = k6gw.Game(5, rounds=1)
    g.new_round()
    for i in range(60):
        s = g.state()
        if s.over:
            break
        me = s.mover
        d = abs(s.p[1].x - s.p[0].x)
        best = None
        for pw in range(30, 101):                # aim by search (the host can)
            t = k6gw.Game(5, rounds=1)
            t.g.raw  # noqa
            import ctypes
            ctypes.memmove(t.g, g.g, len(g.g.raw))
            t.shot(45 if me == 0 else 135, pw, 0)
            hp = t.state().p[me ^ 1].hp
            if best is None or hp < best[0]:
                best = (hp, pw)
        g.shot(45 if me == 0 else 135, best[1], 0)
        del d
    s = g.state()
    assert s.over == 2 and s.winner in (0, 1)
    w = s.winner
    assert s.p[w].wins == 1 and s.p[w].money > s.p[w ^ 1].money


# ---- hot seat ----

@need_clang
@pytest.mark.parametrize("name", [
    "gw",
    "gw_answer",
    "gw_auto",
    "gw_call",
])
def test_builds_and_fits(name):
    from modem import k6mod
    code, entry = build(name)
    assert k6mod.source_level(os.path.join(MOD, name + ".c")) == 6 and entry == 0
    assert len(code) <= BLOCK, len(code)         # (build.sh's limit too)


@need_clang
def test_hot_seat_bot_game():
    ret, said, regs, tx, lcd = play("gw_auto", None, count=1_500_000_000, GW_ROUNDS=1)
    shots = [ln for ln in said if ln.startswith("gw shot")]
    assert ret == 1 and said[-1] == "gw bye 1" and any(ln.startswith("gw end") for ln in said)
    assert len(shots) >= 2 and "log" not in tx                 # hot seat never transmits
    assert {ln.split()[2] for ln in shots} == {"1", "2"}
    assert lcd["st"]["stack"] <= STACK, lcd["st"]["stack"]
    texts = [f["text"][0] for f in lcd["frames"]]
    assert any(t.startswith(">P1") for t in texts) and any(" >P2" in t for t in texts)


def taps(seq, t0, gap=300_000, hold=120_000):
    out, t = [], t0
    for k in seq:
        if isinstance(k, (int, float)):
            t += int(k * 1e6)
            continue
        out.append((t, t + hold, k))
        t += gap
    return out


@need_clang
def test_hot_seat_keys_shop_aim_fire():
    keys = taps(["1", 0.5, "M", "X", 0.5, "D", "M", "X", 1.0, "#", "4", "4", "2", "M", 8,
                 "X", "X"], 1_000_000)
    ret, said, regs, tx, lcd = play("gw", None, keypad=keys, count=60_000_000)
    fr = lcd["frames"]
    shop = [f for f in fr if f["text"][0].startswith("SHOP P1")]
    assert shop and shop[0]["text"][1].startswith(">Kilowatt") and "$80" in shop[0]["text"][0]
    assert any("$20" in f["text"][0] for f in shop)              # bought it
    assert any(f["text"][0].startswith("SHOP P2  $45") for f in fr)    # P2's FCC Violator
    aimed = [f for f in fr if f["text"][5].startswith("Kilowatt x1")]
    assert aimed and aimed[-1]["text"][1].startswith("A047 P061")
    assert "gw shot 1 47 61 1" in said


# ---- radio play ----

@need_clang
@pytest.mark.parametrize("name,lose,k6_lose", [
    ("gw_call", (3,), ()),                       # one of the peer's copies lost
    ("gw_answer", (3,), ()),
    ("gw_call", (4, 5), (2,)),                   # a whole over lost each way: both send again
    ("gw_answer", (6, 7), (3,)),
])
def test_radio_game_against_the_pc(name, lose, k6_lose):
    air = Air(lose=lose, k6_lose=k6_lose)
    if name == "gw_answer":
        air.peer.call(0x2468ACE, rounds=1)
    ret, said, regs, tx, lcd = play(name, air, GW_ROUNDS=1)
    k6 = [d for _, d in air.k6_pkts]
    assert all(d is not None and d["call"] == "AG7EW" for d in k6)
    types = {d["type"] for d in k6}
    assert {k6gw.SHOP, k6gw.MOVE, k6gw.ACK} <= types
    assert k6gw.HELLO in types if name == "gw_call" else k6gw.JOIN in types
    assert air.peer.done and air.peer.desync == 0 and "gw desync" not in said
    tries = [d["seq"] for d in k6 if d["type"] not in (k6gw.ACK,)]
    assert (len(tries) > 2 * len(set(tries))) == bool(k6_lose)       # a repeat only after a loss
    assert ret == 1 and any(ln.startswith("gw end") for ln in said)
    # both radios end with the same game
    end = [ln for ln in said if ln.startswith("gw end")][0].split()[2]
    s = air.peer.game.state()
    assert int(end) == (0 if s.p[0].wins > s.p[1].wins else 1 if s.p[1].wins > s.p[0].wins else 2)
    check_tx_rules(tx, air)
    assert lcd["st"]["stack"] <= STACK, lcd["st"]["stack"]


def check_tx_rules(tx, air):
    ov = overs(tx)
    assert ov and all(n in ("tx_on", "tx_off", "tx_pa") for n, *_ in tx["log"])
    asked = [a for n, a, _ in tx["log"] if n == "tx_on"]
    assert max(asked) <= 10_000
    for t_on, t_off in ov:
        assert (t_off - t_on) / 1000 < 8000
        # never while the peer is on the air
        assert not any(a <= t_on < b for a, b in air.peer_overs)
    assert {r for r, v, us in air.keyed_writes} <= FSK_REGS
    # each over ends in AG7EW, keyed on REG_70 (the tone oscillator)
    for t_on, t_off in ov:
        ev = [(bool(v & 0x8000), us) for r, v, us in tx["w"] if r == 0x70 and t_on <= us <= t_off]
        ev = [e for i, e in enumerate(ev) if i == 0 or e[0] != ev[i - 1][0]]
        ev = [e for e in ev if e[1] >= ev[0][1]] + [(False, t_off)]
        spans = [(on, ev[i + 1][1] - us) for i, (on, us) in enumerate(ev[:-1])]
        assert morse(spans).endswith("AG7EW"), morse(spans)


@need_clang
def test_radio_needs_440_850():
    keys = taps(["2", 1.0, "X"], 500_000)
    air = Air()
    ret, said, regs, tx, lcd = play("gw", air, keypad=keys, count=30_000_000, rx_hz=446_000_000)
    assert "log" not in tx and any(f["text"][5] == "radio: 440.850 only" for f in lcd["frames"])


@need_clang
def test_never_keys_up_on_a_busy_channel():
    # the channel busy for the first 40 s: the caller's HELLO waits, then gives up (15 s a try)
    air = Air(busy=[(0, 40_000_000)])
    ret, said, regs, tx, lcd = play("gw_call", air, count=150_000_000, GW_ROUNDS=1)
    for t_on, _ in overs(tx):
        assert t_on >= 40_000_000
    assert any(ln.endswith("r=-2") for ln in said)


@need_clang
def test_a_key_ends_an_over():
    ret, said, regs, tx, lcd = play("gw_call", Air(), count=40_000_000, GW_ROUNDS=1)
    t_on = overs(tx)[0][0]
    t_key = t_on + TX_ON_US + 200_000              # (in the lead, before the packet)
    keys = [(t_key, t_key + 100_000, "5")]
    air = Air()
    ret, said, regs, tx, lcd = play("gw_call", air, keypad=keys, count=40_000_000, GW_ROUNDS=1)
    t_on2, t_off2 = overs(tx)[0]
    assert t_on2 == t_on and t_off2 < t_key + 100_000
    assert any(ln.endswith("r=-4") for ln in said)
    last = {r: v for r, v, us in tx["w"] if us <= t_off2}    # the modem quiet at the key-down
    assert last.get(0x70, 0) == 0 and last.get(0x58, 0) == 0
