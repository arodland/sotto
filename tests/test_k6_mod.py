"""K6 RAM modules (c/k6/k6_mod.c, modem/k6mod.py, c/k6/mod/hello.c): the packer's SipHash
matches the paper's vector, the loader on this machine (c/k6/km_host, test key 00..0f) takes
a module in any chunk order and refuses a wrong tag, a replay, a bad CRC and a module too big
for the store, and on an emulated Cortex-M0 (unicorn) the thumbv6m loader receives the hello
module and runs it from RAM. Needs make -C c/k6 km_host; the build and emulator tests also
clang, ld.lld and unicorn. SPDX-License-Identifier: MIT"""
import os
import random
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
HOST = os.path.join(K6, "km_host")
HELLO = os.path.join(K6, "mod", "hello.c")
sys.path.insert(0, ROOT)
from modem import k6mod, pager  # noqa: E402

KEY = bytes(range(16))
need_host = pytest.mark.skipif(not os.path.exists(HOST), reason="needs make -C c/k6 km_host")
need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")


def run_host(packets, arena=4096):
    inp = "".join(p.hex() + "\n" for p in packets)
    out = subprocess.run([HOST, str(arena)], input=inp, check=True, capture_output=True, text=True).stdout
    lines = out.split("\n")
    rs = [int(ln.split()[0][2:]) for ln in lines if ln.startswith("r=")]
    tail = [ln for ln in lines if ln.startswith("code_len=")][0].split()
    return rs, int(tail[0].split("=")[1]), int(tail[1].split("=")[1]), bytes.fromhex(lines[-2])


def fake_module(n=300, serial=1000, key=KEY):
    code = bytes(random.Random(n).randrange(256) for _ in range(n))
    return k6mod.image(code, 0, serial, key)


def test_siphash_vector():
    # SipHash paper, appendix A: key 00..0f, message 00..0e
    assert k6mod.siphash24(KEY, bytes(range(15))) == 0xA129CA6149BE45E5


def test_image_and_packets_round_trip():
    img = fake_module(500)
    pk = k6mod.packets(img, 9, 64)
    assert len(pk) == -(-len(img) // 58)
    got = b"".join(k6mod.parse(p)[3] for p in pk)
    assert got[:len(img)] == img
    assert all(k6mod.parse(p)[:3] == (9, i, len(pk)) for i, p in enumerate(pk))


@need_host
@pytest.mark.parametrize("length", [64, 128])
def test_loader_any_order(length):
    img = fake_module(700)
    pk = k6mod.packets(img, 3, length)
    order = pk[1:] + [pk[1], pk[0]]
    random.Random(5).shuffle(order)
    rs, code_len, serial, arena = run_host(order + [pk[2]])
    assert rs.count(3) == 1 and rs[-1] == 2          # ready once, then a repeat is a duplicate
    assert code_len == 700 and serial == 1000
    assert arena == img[:64]


@need_host
def test_loader_refuses():
    img = fake_module()
    pk = k6mod.packets(img, 1, 128)
    rs, *_ = run_host(k6mod.packets(fake_module(key=bytes(16)), 1, 128))
    assert rs[-1] == -4                              # someone else's key
    bad = bytearray(pk[1])
    bad[40] ^= 1
    rs, *_ = run_host([pk[0], bytes(bad), pk[1], pk[2]])
    assert rs[1] == -1 and rs[-1] == 3                # a bad CRC costs only that packet
    rs, *_ = run_host(pk + k6mod.packets(fake_module(serial=1000), 2, 128)
                      + k6mod.packets(fake_module(serial=999), 4, 128))
    assert rs[2] == 3 and rs[5] == -6 and rs[8] == -6     # same or older serial: a replay
    rs, *_ = run_host(pk, arena=200)
    assert -2 in rs and 3 not in rs                  # doesn't fit the store
    rs, *_ = run_host(pk[:2])
    assert 3 not in rs                               # a chunk missing: nothing runs
    voice = pager.profile("XL", "2400").pack(bytes(6 * 160), 5)[0]
    rs, *_ = run_host([voice])
    assert rs == [0]                                 # not a module packet: the pager's


@need_clang
def test_hello_builds_position_independent():
    code, entry = k6mod.build(HELLO)
    assert 100 < len(code) < 1024 and entry % 2 == 0
    img = k6mod.image(code, entry, 1, KEY)
    assert len(k6mod.packets(img, 0, 128)) <= 4      # about 3 s on air at F/1800 XL


@need_clang
def test_not_position_independent_is_refused():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "bad.c")
        with open(src, "w") as f:   # a table of absolute pointers: needs relocation
            f.write('#include "k6_mod.h"\nstatic const char a[] = "x";\n'
                    'static const char *const t[] = {a, a + 1};\n'
                    'int k6_main(const k6_api_t *api) { api->say(t[api->abi & 1]); return 0; }\n')
        with pytest.raises(Exception):
            k6mod.build(src)


# ---- the loader and the hello module on an emulated Cortex-M0 ----

def _loader_elf(d, keyed=True):
    res = subprocess.run(["clang", "-print-resource-dir"], capture_output=True, text=True, check=True).stdout.strip()
    fl = ["--target=thumbv6m-none-eabi", "-mcpu=cortex-m0", "-mthumb", "-mfloat-abi=soft", "-Os", "-nostdinc",
          "-isystem", os.path.join(ROOT, "c", "fixed", "cm4", "inc"), "-isystem", os.path.join(res, "include"),
          "-ffunction-sections", "-fdata-sections", "-std=c99", "-ffreestanding", "-fno-builtin",
          "-DSOTTO_V36", "-DSOTTO_PITCH_THIRD", "-DFX_CS512_CONST", "-I" + os.path.join(ROOT, "c"),
          *(["-DKM_KEY=" + ",".join(str(b) for b in KEY)] if keyed else [])]
    objs = []
    for src in ("c/k6/k6_mod.c", "c/k6/sotto_pager.c", "c/fixed/cm0/rt.c", "c/fixed/cm4/shim.c"):
        o = os.path.join(d, os.path.basename(src) + ".o")
        subprocess.run(["clang", *fl, "-c", os.path.join(ROOT, src), "-o", o], check=True)
        objs.append(o)
    o = os.path.join(d, "divmod.o")
    subprocess.run(["clang", "--target=thumbv6m-none-eabi", "-mcpu=cortex-m0", "-c",
                    os.path.join(ROOT, "c/fixed/cm0/divmod.s"), "-o", o], check=True)
    elf = os.path.join(d, "loader.elf")
    subprocess.run(["ld.lld", "-T", os.path.join(ROOT, "c/fixed/cm4/link.ld"), "--gc-sections",
                    "-e", "km_packet", "-u", "km_run", *objs, o, "-o", elf], check=True)
    return elf


def test_key_image():
    img = b"\0" * 100 + k6mod.PLACEHOLDER + b"\1" * 50
    assert k6mod.key_image(img, KEY) == b"\0" * 100 + KEY + b"\1" * 50
    with pytest.raises(ValueError):
        k6mod.key_image(img + k6mod.PLACEHOLDER, KEY)


@need_clang
@pytest.mark.parametrize("how", ["built-with-key", "keyimage", "no-key"])
def test_hello_runs_from_ram_on_m0(how):
    unicorn = pytest.importorskip("unicorn")
    from unicorn import arm_const as A
    sys.path.insert(0, os.path.join(ROOT, "c", "fixed", "cm4"))
    import run as cm4  # noqa: E402  (symbols, flash_image)

    code, entry = k6mod.build(HELLO)
    pk = k6mod.packets(k6mod.image(code, entry, 7, KEY), 1, 128)
    with tempfile.TemporaryDirectory() as d:
        elf = _loader_elf(d, keyed=how == "built-with-key")
        sym, img = cm4.symbols(elf), cm4.flash_image(elf)
    if how == "keyimage":           # CI's unkeyed image, keyed afterwards (modem.k6 keyimage)
        img = k6mod.key_image(img, KEY)
    mu = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB | unicorn.UC_MODE_MCLASS)
    mu.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M0)
    flash, ram = 0x08000000, 0x20000000
    mu.mem_map(flash, 1 << 20)
    mu.mem_map(ram, 1 << 20)
    mu.mem_write(flash, img)
    stop = flash + (1 << 20) - 0x100
    mu.mem_write(stop, b"\x00\xbf" * 8)
    arena, pkt, api, say, ticks = ram + 0x40000, ram + 0x50000, ram + 0x51000, ram + 0x51100, ram + 0x51200
    mu.mem_write(say, b"\x70\x47")                   # bx lr
    mu.mem_write(ticks, b"\x20\x20\x70\x47")         # movs r0, #0x20; bx lr
    mu.mem_write(api, (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
                 + (say | 1).to_bytes(4, "little") + (ticks | 1).to_bytes(4, "little"))
    said = []

    def on_say(uc, addr, size, _):
        p = uc.reg_read(A.UC_ARM_REG_R0)
        said.append(bytes(uc.mem_read(p, 64)).split(b"\0")[0].decode())
    mu.hook_add(unicorn.UC_HOOK_CODE, on_say, begin=say, end=say + 1)

    def call(fn, *args):
        for r, v in zip((A.UC_ARM_REG_R0, A.UC_ARM_REG_R1, A.UC_ARM_REG_R2, A.UC_ARM_REG_R3), args):
            mu.reg_write(r, v)
        mu.reg_write(A.UC_ARM_REG_SP, ram + 0x3FF00)
        mu.reg_write(A.UC_ARM_REG_LR, stop | 1)
        mu.emu_start(sym[fn] | 1, stop, count=2_000_000)
        return mu.reg_read(A.UC_ARM_REG_R0)

    rs = []
    for p in pk:
        mu.mem_write(pkt, p)
        r = call("km_packet", pkt, len(p), arena, 4096)
        rs.append(r - (1 << 32) if r & 0x80000000 else r)
    if how == "no-key":
        assert rs == [1] * (len(pk) - 1) + [-5] and call("km_run", arena, api) == (-100) & 0xFFFFFFFF
        return
    assert rs == [1] * (len(pk) - 1) + [3]
    assert call("km_run", arena, api) == 0x6B
    sq = sum(i * i for i in range(1, 10001)) & 0xFFFFFFFF
    assert said == [f"hello world from RAM at 0x{arena + 16 + entry | 1:08x}",
                    f"sum of squares 0x{sq:08x} in 0x0000 ticks of 100 us"]
    assert call("km_run", arena, api) == (-100) & 0xFFFFFFFF   # once only


# ---- the settle-time module (c/k6/mod/settle.c, API level 2) on an emulated M0 ----

SETTLE = os.path.join(K6, "mod", "settle.c")


@need_host
def test_loader_takes_level_3_refuses_4():
    code = bytes(64)
    for level in (2, 3):
        rs, *_ = run_host(k6mod.packets(k6mod.image(code, 0, 1000 + level, KEY, level=level), 1, 128))
        assert rs[-1] == 3
    rs, *_ = run_host(k6mod.packets(k6mod.image(code, 0, 1000, KEY, level=4), 1, 128))
    assert 3 not in rs


@need_clang
def test_settle_declares_level_2():
    assert k6mod.source_level(SETTLE) == 2 and k6mod.source_level(HELLO) == 1


@need_clang
@pytest.mark.parametrize("sender_tail_us", [0, 3_000_000])
def test_settle_measures_a_modelled_receiver(sender_tail_us):
    """The module, run straight from RAM, against a receiver model: a carrier on F that comes
    on 2 s in; after a tune RSSI reads the noise floor until the model's settle time (700 us
    wide, 1500 us narrow) and the true level after; going off F it drops to the floor in
    300 us. The module must report those times (the first t of its grid at or after them)."""
    unicorn = pytest.importorskip("unicorn")
    from unicorn import arm_const as A
    code, entry = k6mod.build(SETTLE)
    assert entry == 0 and len(code) < 4096
    F, NOISE, SIG = 440_850_000, -118, -61
    SETTLE_US = {1: 700, 0: 1500}
    st = {"us": 0, "f": F, "wide": 1, "t_tune": 0, "said": [], "tunes": 0, "writes": 0}

    def level():
        if st["us"] < sender_tail_us:
            return -21                               # the sender's module carrier, still up
        on = st["us"] >= sender_tail_us + 2_000_000
        since = st["us"] - st["t_tune"]
        if st["f"] == F and on:
            return SIG if since >= SETTLE_US[st["wide"]] else NOISE
        if st["f"] != F and on and since < 300 and st.get("prev") == F:
            return SIG                               # still reads the carrier just after leaving
        return NOISE

    mu = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB | unicorn.UC_MODE_MCLASS)
    mu.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M0)
    ram = 0x20000000
    mu.mem_map(ram, 1 << 20)
    base, api, stubs, stop = ram + 0x1000, ram + 0x8000, ram + 0x9000, ram + 0x9F00
    mu.mem_write(base, code)
    names = ["say", "ticks", "fd_read", "fd_write", "delay_us", "tune", "rx_hz", "ms", "key_down", "scratch"]
    for i in range(len(names)):
        mu.mem_write(stubs + 16 * i, b"\x70\x47")    # bx lr; the hook does the work
    mu.mem_write(stop, b"\x00\xbf" * 8)
    mu.mem_write(api, (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
                 + b"".join((stubs + 16 * i | 1).to_bytes(4, "little") for i in range(len(names))))

    def on_stub(uc, addr, size, _):
        name = names[(addr - stubs) // 16]
        r0, r1 = uc.reg_read(A.UC_ARM_REG_R0), uc.reg_read(A.UC_ARM_REG_R1)
        ret = 0
        if name == "say":
            st["said"].append(bytes(uc.mem_read(r0, 64)).split(b"\0")[0].decode())
        elif name == "ticks":
            ret = (st["us"] // 100) & 0xFFFF
        elif name == "fd_read":
            st["us"] += 20                           # an SPI register read
            ret = ((level() + 174) << 1) if r0 == 0x67 else 0x1234
        elif name == "fd_write":
            st["writes"] += 1
        elif name == "delay_us":
            st["us"] += r0
        elif name == "tune":
            st["prev"], st["f"], st["wide"], st["t_tune"] = st["f"], r0, r1, st["us"]
            st["tunes"] += 1
        elif name == "rx_hz":
            ret = F
        elif name == "ms":
            ret = st["us"] // 1000
        elif name == "scratch":
            ret = ram + 0x6000
            if r0:
                uc.mem_write(r0, (1024).to_bytes(4, "little"))
        uc.reg_write(A.UC_ARM_REG_R0, ret & 0xFFFFFFFF)
    mu.hook_add(unicorn.UC_HOOK_CODE, on_stub, begin=stubs, end=stubs + 16 * len(names))
    mu.reg_write(A.UC_ARM_REG_R0, api)
    mu.reg_write(A.UC_ARM_REG_SP, ram + 0x7F00)
    mu.reg_write(A.UC_ARM_REG_LR, stop | 1)
    mu.emu_start(base + entry | 1, stop, count=20_000_000)
    ret = mu.reg_read(A.UC_ARM_REG_R0)
    said = st["said"]
    lines = {ln.split(" ref")[0]: ln for ln in said if ln.startswith("settle ") and " ref " in ln}
    for w, c in (("W", 1), ("N", 0)):
        for case in ("10k", "1M", "band"):
            assert lines[f"settle {case} {w}"] == f"settle {case} {w} ref {SIG} dBm {SETTLE_US[c]} us"
        assert lines[f"settle off {w}"] == f"settle off {w} ref {NOISE} dBm 400 us"
    assert ret == 1500                               # the worst on-case: narrow
    if sender_tail_us:
        assert said.pop(0) == "settle: waiting for the sender's carrier to drop"
    assert said[0] == f"settle: waiting for a carrier on 440.850 MHz, now {NOISE} dBm"
    assert any(ln.startswith("curve 10k W 0:-118 100:-118") for ln in said)
    assert all(len(ln) <= 60 for ln in said)
    assert st["writes"] == 0 and st["tunes"] > 400
    assert said[-1].startswith("settle: done in ") and said[-1].endswith("worst on-case 1500 us")
