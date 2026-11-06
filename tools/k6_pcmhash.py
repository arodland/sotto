#!/usr/bin/env python3
"""What the K6 should print as pcm= / samples= in its PLAY line for a message, so a playback can
be checked over the serial report without anyone listening.

  python3 tools/k6_pcmhash.py msg.sot --codec 1200 --payload XL
  python3 tools/k6_pcmhash.py msg.sot --codec 1200 --payload XL --missing 3,17
  python3 tools/k6_pcmhash.py msg.sot --codec 1200 --payload XL --check 'K6 PLAY id=.. pcm=..'

It packs the .sot exactly as `python -m modem.k6 tx` does (same profile, msg_id), leaves out the
packets the K6 reported missing, and plays the rest through c/k6/pager_host (v3.7),
c/k6/pager_host_lr (sotto 1200) or c/k6/pager_host_vlr (sotto 600 / 400): the same C as the
radio's decoder, bit-exact with it, built for the PC. The hash is FNV-1a over the int16 samples before the PWM conversion (sp_pcm_hash in
c/k6/sotto_pager.h). With --check it reads the K6 PLAY line, takes its missing= list, and says
MATCH or MISMATCH (exit 1). Build the hosts first: make -C c/k6.

A MATCH means the radio decoded every sample it played exactly as the reference: the radio side
works, and what is left for ears is how the codec sounds. A missing list longer than the 8 the
K6 prints can't be reproduced; the tool says so.
SPDX-License-Identifier: MIT
"""
import argparse
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modem import pager  # noqa: E402
from modem.k6 import msg_id_for  # noqa: E402

K6 = os.path.join(ROOT, "c", "k6")


def reference(frames, codec="2400", payload="XL", missing=(), msg_id=None, store=200000, depth=8):
    """(hash as 8 hex digits, samples, the host's status dict) for frames with the packets in
    missing left out."""
    prof = pager.profile(payload, codec, depth)
    mid = msg_id if msg_id is not None else msg_id_for(frames)
    pk = [p for i, p in enumerate(prof.pack(frames, mid)) if i not in set(missing)]
    host = os.path.join(K6, {"1200": "pager_host_lr", "600": "pager_host_vlr", "400": "pager_host_vlr"}
                        .get(codec, "pager_host"))
    if not os.path.exists(host):
        sys.exit(f"{host} not built: make -C c/k6")
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(pk))
        # store then play, in a store big enough for the whole message: a streaming radio decodes
        # the same frames with the same good flags (tests/test_k6_pager.py checks it)
        args = [host, "-S"] + (["-x"] if payload == "XL" else []) + ["-b", str(store), f"{d}/p.bin", f"{d}/o.raw"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
    st = dict(kv.split("=", 1) for kv in out if "=" in kv)
    return st["pcm"], int(st["samples"]), st


def parse_play(line):
    """The k=v fields of a 'K6 PLAY ...' line, and its missing list (None if it was cut at 8)."""
    kv = dict(w.split("=", 1) for w in line.split() if "=" in w)
    m = kv.get("missing", "0")
    n, _, lst = m.partition(":")
    seqs = [int(x) for x in lst.split(",") if x]
    return kv, (seqs if int(n) == len(seqs) else None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sot", help="the .sot that was sent")
    ap.add_argument("--codec", choices=("2400", "1200", "600", "400"), default="2400")
    ap.add_argument("--payload", choices=("L", "XL"), default="XL")
    ap.add_argument("--depth", type=int, choices=pager.DEPTHS, default=8, help="as given to modem.k6")
    ap.add_argument("--msg-id", type=int, help="as given to modem.k6 (default: from the frames, as it does)")
    ap.add_argument("--missing", default="", help="packet seqs the K6 reported missing, e.g. 3,17")
    ap.add_argument("--check", metavar="LINE", help="the K6 PLAY line to compare with")
    a = ap.parse_args(argv)
    frames = open(a.sot, "rb").read()
    missing = [int(x) for x in a.missing.split(",") if x]
    kv = None
    if a.check:
        kv, seqs = parse_play(a.check)
        if seqs is None:
            sys.exit("the PLAY line lists only the first 8 missing packets; can't rebuild the rest")
        missing = seqs
        if "pcm" not in kv:
            sys.exit("no pcm= in that PLAY line (firmware before the checksum)")
    h, n, st = reference(frames, a.codec, a.payload, missing, a.msg_id, depth=a.depth)
    print(f"pcm={h} samples={n} total={st['total']} got={st['got']} missing={','.join(map(str, missing)) or '-'}")
    if kv is not None:
        ok = kv["pcm"] == h and int(kv["samples"]) == n
        why = "" if ok else f" (K6 pcm={kv['pcm']} samples={kv['samples']})"
        if not ok and kv.get("aborted", "0") != "0":
            why += ", the K6 aborted the playback"
        print(("MATCH" if ok else "MISMATCH") + why)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
