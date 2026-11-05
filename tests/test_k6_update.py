"""tools/k6_update.py: the bootloader handshake frame, reply detection, and the reset-then-
handshake sequence against a fake K6 port (no serial hardware)."""
import importlib.util
import os
import time

spec = importlib.util.spec_from_file_location(
    "k6_update", os.path.join(os.path.dirname(__file__), "..", "tools", "k6_update.py"))
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


def test_handshake_frame():
    f = u.HANDSHAKE
    assert len(f) == 8 and f[:5] == bytes([0xAA, 0x42, 0, 0, 0]) and f[7] == 0xEF
    assert u.crc16_xmodem(b"123456789") == 0x31C3      # CRC-16/XMODEM check value


def test_find_reply():
    ack = u.frame(0x42, 6)
    assert u.find_reply(b"K6 PKT n=3\r\n" + ack)
    assert not u.find_reply(ack[:-1])
    assert not u.find_reply(ack[:5] + b"\x00\x00\xef")      # bad CRC


class FakePort:
    """Prints K6 lines, then (after reset) answers the first handshake inside its window."""

    def __init__(self, lines, window_after_write=0):
        self.rx = b"".join(l.encode() + b"\r\n" for l in lines)
        self.writes = []
        self.answer_after = window_after_write

    def read(self, n):
        b, self.rx = self.rx[:n], self.rx[n:]
        if not b:
            time.sleep(0.001)
        return b

    def write(self, b):
        self.writes.append(b)
        if len(self.writes) > self.answer_after:
            self.rx += u.frame(0x42, 6)


def test_reset_then_window():
    p = FakePort(["K6 CFG why=tick", "K6 CTL seq=9 action=7", "K6 RESET seq=9"], window_after_write=2)
    out = []
    assert u.wait_reset_line(p, 1.0, out.append)
    assert p.writes == []                       # nothing written before the reset line
    assert u.catch_window(p, 1.0)
    assert len(p.writes) == 3 and all(w == u.HANDSHAKE for w in p.writes)


def test_no_reset_line_writes_nothing():
    p = FakePort(["K6 CFG why=tick"])
    assert not u.wait_reset_line(p, 0.1, lambda s: None)
    assert p.writes == []
