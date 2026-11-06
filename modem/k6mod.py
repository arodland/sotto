"""K6 RAM modules: build a module, sign it, and cut it into 0xAB packets (c/k6/k6_mod.h).

  python -m modem.k6 modkey ~/.config/sotto/k6-mod.key     a new random 16-byte key (hex)
  K6_MOD_KEY_FILE=~/.config/sotto/k6-mod.key sh firmware/k6-aura/build.sh
                                                           an image that accepts modules
  python -m modem.k6 mod hello.wav c/k6/mod/hello.c --key-file ~/.config/sotto/k6-mod.key \\
      --phy F --payload XL                                 the module as transmit audio

The module is C built with clang for the M0 with -fropi, so its code and constants are reached
PC-relative and it runs wherever the K6 puts it. build() links it at two addresses and refuses
it unless both come out byte for byte the same. Image layout and packet format: k6_mod.h and
docs/k6-modules.md. SPDX-License-Identifier: MIT
"""
import os
import re
import secrets
import struct
import subprocess
import tempfile

from . import pager

MAGIC = 0xAB
ABI = 1
LEVEL = 1
HDR = 16
TAG = 8
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
LINK = os.path.join(ROOT, "c", "k6", "mod", "module.ld")
CFLAGS = ["--target=thumbv6m-none-eabi", "-mcpu=cortex-m0", "-mthumb", "-mfloat-abi=soft", "-Os",
          "-fropi", "-ffreestanding", "-fno-builtin", "-nostdlib", "-ffunction-sections",
          "-fdata-sections", "-Wall", "-Werror", "-I" + os.path.join(ROOT, "c", "k6")]
ENTRY = "k6_main"

_M = (1 << 64) - 1


def _rotl(x, b):
    return ((x << b) | (x >> (64 - b))) & _M


def _rounds(v, n):
    for _ in range(n):
        v[0] = (v[0] + v[1]) & _M; v[1] = _rotl(v[1], 13) ^ v[0]; v[0] = _rotl(v[0], 32)
        v[2] = (v[2] + v[3]) & _M; v[3] = _rotl(v[3], 16) ^ v[2]
        v[0] = (v[0] + v[3]) & _M; v[3] = _rotl(v[3], 21) ^ v[0]
        v[2] = (v[2] + v[1]) & _M; v[1] = _rotl(v[1], 17) ^ v[2]; v[2] = _rotl(v[2], 32)


def siphash24(key, m):
    """SipHash-2-4 of bytes m under a 16-byte key, as an int (the K6 compares it little-endian)."""
    k0, k1 = struct.unpack("<QQ", bytes(key))
    v = [k0 ^ 0x736F6D6570736575, k1 ^ 0x646F72616E646F6D, k0 ^ 0x6C7967656E657261, k1 ^ 0x7465646279746573]
    m = bytes(m)
    n = len(m) - len(m) % 8
    for i in range(0, n, 8):
        mi = struct.unpack_from("<Q", m, i)[0]
        v[3] ^= mi; _rounds(v, 2); v[0] ^= mi
    b = (len(m) & 0xFF) << 56 | int.from_bytes(m[n:], "little")
    v[3] ^= b; _rounds(v, 2); v[0] ^= b
    v[2] ^= 0xFF; _rounds(v, 4)
    return v[0] ^ v[1] ^ v[2] ^ v[3]


def new_key(path):
    """Write a random key as 32 hex digits, readable by you only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(16) + "\n")


def read_key(path):
    k = bytes.fromhex(open(os.path.expanduser(path)).read().strip())
    if len(k) != 16:
        raise ValueError(f"{path}: want 32 hex digits (16 bytes), got {len(k)} bytes")
    return k


def build(src, cc="clang", ld="ld.lld", objcopy="llvm-objcopy", nm="llvm-nm"):
    """C source -> (code bytes, entry offset). Refuses code that isn't position-independent."""
    with tempfile.TemporaryDirectory() as d:
        obj = os.path.join(d, "m.o")
        subprocess.run([cc, *CFLAGS, "-c", src, "-o", obj], check=True)
        bins = []
        for base in (0, 0x1000):
            elf = os.path.join(d, f"m{base}.elf")
            subprocess.run([ld, "-T", LINK, "--defsym", f"MODULE_BASE={base}", "--gc-sections",
                            "--no-undefined", obj, "-o", elf], check=True)
            out = os.path.join(d, f"m{base}.bin")
            subprocess.run([objcopy, "-O", "binary", "--only-section=.text", elf, out], check=True)
            bins.append(open(out, "rb").read())
            if base == 0:
                syms = subprocess.run([nm, elf], check=True, capture_output=True, text=True).stdout
        if bins[0] != bins[1]:
            raise ValueError(f"{src}: not position-independent (linked at 0 and 0x1000 the bytes differ)")
        entry = next(int(ln.split()[0], 16) for ln in syms.splitlines() if ln.split()[-1] == ENTRY)
    return bins[0], entry


def source_level(src):
    """The API level a module's C source declares (#define K6_MODULE_LEVEL n), or 1."""
    m = re.search(r"^\s*#\s*define\s+K6_MODULE_LEVEL\s+(\d+)", open(src).read(), re.M)
    return int(m.group(1)) if m else LEVEL


def image(code, entry, serial, key, level=LEVEL):
    """Header + code + SipHash tag (k6_mod.h)."""
    code = bytes(code)
    if entry >= len(code) or entry & 1 or len(code) > 0xFFFF:
        raise ValueError("bad entry offset or code length")
    hdr = b"K6M" + bytes([ABI]) + struct.pack("<HHIBBH", len(code), entry, serial & 0xFFFFFFFF, level, 0, 0)
    body = hdr + code
    return body + struct.pack("<Q", siphash24(key, body))


def data_bytes(length):
    return length - 6


def packets(img, xfer, length=128):
    """The image as whitened module packets (length 64 = L, 128 = XL), chunk 0 first."""
    dl = data_bytes(length)
    n = -(-len(img) // dl)
    if n > 255:
        raise ValueError(f"{len(img)} bytes is {n} chunks; 255 at most")
    out = []
    for i in range(n):
        d = bytes([MAGIC, xfer & 0xFF, i, n]) + img[i * dl:(i + 1) * dl].ljust(dl, b"\0")
        d += struct.pack(">H", pager.crc16_ccitt(d))
        out.append(pager._whiten(d))
    return out


def parse(payload):
    """(transfer id, chunk, count, data) of a whitened module packet, or None."""
    d = pager._whiten(payload)
    if d[0] != MAGIC or pager.crc16_ccitt(d[:-2]) != struct.unpack(">H", d[-2:])[0]:
        return None
    return d[1], d[2], d[3], d[4:-2]


PLACEHOLDER = b"sotto-K6MOD-key!"     # k6_mod.c's key in an image built without K6_MOD_KEY_FILE


def key_image(data, key):
    """A K6 image (.bin bytes) built without a key, with the key written over the placeholder."""
    if len(key) != 16:
        raise ValueError("want a 16-byte key")
    n = data.count(PLACEHOLDER)
    if n != 1:
        raise ValueError(f"the key placeholder is in the image {n} times, want once "
                         "(an image built with K6_MOD_KEY_FILE, or without the module loader?)")
    return data.replace(PLACEHOLDER, bytes(key))
