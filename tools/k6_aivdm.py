#!/usr/bin/env python3
"""The K6's AIS sentences (c/k6/mod/aisships.c says each frame as !AIVDM) out of a serial log,
to a chart plotter: OpenCPN, for one, takes NMEA over UDP (Connections: UDP, port 10110).

  tail -F ~/sotto-work/runs/<run>/serial.log | python3 tools/k6_aivdm.py --udp 127.0.0.1:10110
  python3 tools/k6_aivdm.py LOG                print the sentences only

A line counts when it holds a whole sentence with a good checksum, whatever comes before it (the
logger's time stamp and "K6 MOD say "). Standard library only. Opens no serial port."""
import argparse
import re
import socket
import sys

SENT = re.compile(r"(!AIVD[MO],[^*\s]*\*([0-9A-F]{2}))")


def sentence(line):
    """the !AIVDM sentence in a log line, or None (none, or a bad checksum)"""
    m = SENT.search(line)
    if not m:
        return None
    s = m.group(1)
    x = 0
    for c in s[1:s.index("*")]:
        x ^= ord(c)
    return s if x == int(m.group(2), 16) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("log", nargs="?", help="a log file (default: stdin)")
    ap.add_argument("--udp", help="host:port to send each sentence to")
    a = ap.parse_args()
    out = None
    if a.udp:
        host, port = a.udp.rsplit(":", 1)
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dest = (host, int(port))
    fh = open(a.log, errors="replace") if a.log else sys.stdin
    for line in fh:
        s = sentence(line)
        if not s:
            continue
        if out:
            out.sendto((s + "\r\n").encode(), dest)
        else:
            print(s, flush=True)


if __name__ == "__main__":
    main()
