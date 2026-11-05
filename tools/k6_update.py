#!/usr/bin/env python3
"""Flash the K6 without holding the side keys: reboot it over the air, catch the stock
bootloader's UPDATE window on the cable, then run Aura's flash.py.

After every reset the stock bootloader listens on its UART (the K6 cable, 115200 8N1) for
about 200 ms. If it gets one handshake frame (AA 42 00 00 00 CRC16-XMODEM EF) it answers with
a 0x42 frame and stays in UPDATE mode, as if Side Keys 1+2 had been held. Otherwise it boots
the app. The VOICE MSG image reboots on control action 7 (`modem.k6 ctl --action reset`):
it prints `K6 RESET seq=N`, waits 30 ms and resets.

  python tools/k6_update.py IMAGE --trigger "CMD that sends the reset control packet"
  python tools/k6_update.py IMAGE --manual     # K6 OFF first; power it on (no keys) when told

CAUTION: while the pager runs, the cable's PC->K6 wire is the PTT line (PA10). This tool
only writes after the `K6 RESET` line (or, with --manual, while you power the radio on), and
for at most --window seconds. Close any serial listener on the port first.
"""
import argparse
import os
import subprocess
import sys
import time

HANDSHAKE_CMD = 0x42


def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
        crc &= 0xFFFF
    return crc


def frame(cmd, arg=0, data=b""):
    body = bytes([cmd, arg, len(data) >> 8, len(data) & 0xFF]) + data
    c = crc16_xmodem(body)
    return bytes([0xAA]) + body + bytes([c >> 8, c & 0xFF, 0xEF])


HANDSHAKE = frame(HANDSHAKE_CMD)


def find_reply(buf):
    """True if buf holds a well-formed 0x42 frame from the bootloader."""
    i = buf.find(b"\xaa\x42")
    while i >= 0:
        f = buf[i:i + 8]
        if len(f) == 8 and f[3:5] == b"\x00\x00" and f[7] == 0xEF \
                and crc16_xmodem(f[1:5]) == (f[5] << 8 | f[6]):
            return True
        i = buf.find(b"\xaa\x42", i + 1)
    return False


def wait_reset_line(port, timeout, out):
    """Read the K6's report lines until `K6 RESET`; True when seen."""
    deadline, line = time.monotonic() + timeout, b""
    while time.monotonic() < deadline:
        b = port.read(256)
        if not b:
            continue
        line += b
        while b"\n" in line:
            text, line = line.split(b"\n", 1)
            text = text.decode(errors="replace").strip()
            if text:
                out("  K6: " + text[:100])
            if text.startswith("K6 RESET"):
                return True
    return False


def catch_window(port, window, period=0.018):
    """Send the handshake every `period` s (the bootloader ends a frame after 10 ms idle) until
    it answers; True when it does."""
    deadline, buf = time.monotonic() + window, b""
    while time.monotonic() < deadline:
        port.write(HANDSHAKE)
        t = time.monotonic() + period
        while time.monotonic() < t:
            buf += port.read(64)
            if find_reply(buf):
                return True
        buf = buf[-16:]
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", help="firmware .bin for flash.py")
    ap.add_argument("--port", default="/dev/ttyUSB2", help="the K6 cable (default /dev/ttyUSB2)")
    ap.add_argument("--aura", default=os.path.expanduser("~/bf-k61-firmware-aura"),
                    help="Aura checkout with tools/flash.py")
    ap.add_argument("--trigger", help="shell command that transmits the reset control packet "
                                      "(run after the port is open)")
    ap.add_argument("--manual", action="store_true",
                    help="no reset packet: start with the K6 OFF, then power it on (no keys)")
    ap.add_argument("--wait", type=float, default=90.0, help="how long to wait for K6 RESET, s")
    ap.add_argument("--window", type=float, default=1.5, help="how long to send handshakes, s")
    ap.add_argument("--no-flash", action="store_true", help="stop once the K6 is in UPDATE mode")
    a = ap.parse_args(argv)
    out = print
    import serial
    flasher = os.path.join(a.aura, "tools", "flash.py")
    if not a.no_flash and not os.path.exists(flasher):
        ap.error(f"no {flasher}; pass --aura")
    port = serial.Serial(a.port, 115200, timeout=0.002)
    try:
        port.reset_input_buffer()
        if a.manual:
            input("K6 OFF? Press Enter, then power it on (no keys) within 10 s... ")
            ok = catch_window(port, 10.0)
        else:
            proc = None
            if a.trigger:
                out(f"trigger: {a.trigger}")
                proc = subprocess.Popen(a.trigger, shell=True)
            else:
                out("send the reset control packet now (modem.k6 ctl --phy F --action reset)")
            if not wait_reset_line(port, a.wait, out):
                out("no K6 RESET line: nothing was written to the cable")
                return 1
            time.sleep(0.04)           # the K6 waits 30 ms, then resets
            ok = catch_window(port, a.window)
            if proc:
                proc.wait()
    finally:
        port.close()
    if not ok:
        out("no answer from the bootloader: the K6 has booted the pager again; "
            "use Side Keys 1+2 and flash.py")
        return 1
    out("K6 is in UPDATE mode")
    if a.no_flash:
        return 0
    return subprocess.call([sys.executable, flasher, a.image, a.port])


if __name__ == "__main__":
    sys.exit(main())
