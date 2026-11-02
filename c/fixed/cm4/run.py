"""Cycle estimate for the fixed-point sotto on a Cortex-M4 (STM32F405 class: TYT MD-UV380/390,
Module17, CS7000), from an instruction-level emulator and a static timing model.

Usage: python c/fixed/cm4/run.py BUILD/bench.elf IN.raw|IN.wav [--16k] [--frames N] [--profile]
       [--flash] [--bits OUT.bit] [--pcm OUT.raw] [--json OUT.json]

Unicorn runs the bare-metal build from tools/cm4_build.sh one sotto_encode / sotto_decode call
at a time (8 kHz, 160 samples a frame); every executed basic block is costed with the
Cortex-M4 timings below and reported as MHz for real time (cycles per 20 ms frame x 50).
--16k runs the v3.8 16 kHz encoder and decoder instead (sotto_encode16 / sotto_decode16, 320
samples a frame; build with VARIANT=v3.8 sh tools/cm4_build.sh).
--bits writes the encoder's bitstream, which must equal the x86 build's (c/fixed/sotto_enc_fx
[-16]); --pcm writes the decoder's output as sotto_dec_fx [-16] would (its delay removed).

Timing model (ARM Cortex-M4 TRM, table 3-1), zero wait states:
  data processing, multiplies and multiply-accumulates incl. SMULL/SMLAL/SMLAD: 1
  LDR/STR (any width): 2, or 1 right after another single load/store (pipelined)
  LDRD/STRD: 3; LDM/STM/PUSH/POP: 1 + registers (+2 when POP loads pc)
  SDIV/UDIV: 7 (2..12); TBB/TBH: 4; B/BL/BX/BLX: 3; conditional branches: 1, +2 if taken
It ignores wait states and bus stalls, so treat it as +-15 %. --flash adds a sampled model
of the STM32F4's flash at 168 MHz (5 wait states, ART accelerator: 64 x 16-byte instruction
lines with next-line prefetch, 8 x 16-byte data lines): the extra MHz when the tables stay
in flash instead of RAM.
Needs: pip install unicorn capstone.  SPDX-License-Identifier: MIT
"""
import argparse, bisect, collections, json, os, subprocess, sys, tempfile, wave
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_BLOCK, UC_HOOK_MEM_READ
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_SP, UC_ARM_REG_LR)
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS

FLASH, RAM, IO = 0x08000000, 0x20000000, 0x60000000
STACK = 0x4000            # bytes painted below the initial sp to find the stack's high-water mark
WS = 5                    # STM32F4 flash wait states at 168 MHz
ONE_CYCLE_BRANCHES = ('cbz', 'cbnz')


def symbols(elf):
    out = subprocess.run(['llvm-nm-18' if subprocess.run(['which', 'llvm-nm-18'], capture_output=True).returncode == 0 else 'nm', elf],
                         capture_output=True, text=True, check=True).stdout
    sym = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3:
            sym[p[2]] = int(p[0], 16)
    return sym


def flash_image(elf):
    objcopy = 'llvm-objcopy-18' if subprocess.run(['which', 'llvm-objcopy-18'], capture_output=True).returncode == 0 else 'llvm-objcopy'
    with tempfile.TemporaryDirectory() as d:
        subprocess.run([objcopy, '-O', 'binary', '--only-section=.text', '--only-section=.rodata', elf, f'{d}/img.bin'], check=True)
        return open(f'{d}/img.bin', 'rb').read()


def is_cond_branch(m):
    m = m.split('.')[0]
    return m in ONE_CYCLE_BRANCHES or (m.startswith('b') and len(m) == 3 and m not in ('bic', 'bfi', 'bfc', 'bkp'))


def insn_cycles(m, op, prevmem):
    """cycles of one instruction (conditional branches: not taken), and whether it is a single load/store"""
    m = m.split('.')[0]
    if m in ('ldrd', 'strd'):
        return 3, False
    if m.startswith('ldm') or m.startswith('stm') or m in ('push', 'pop'):
        return 1 + op.count(',') + 1 + (2 if m == 'pop' and 'pc' in op else 0), False
    if m.startswith('ldr') or m.startswith('str'):
        return (1 if prevmem else 2) + (2 if m.startswith('ldr') and op.startswith('pc') else 0), True
    if m in ('sdiv', 'udiv'):
        return 7, False
    if m in ('tbb', 'tbh'):
        return 4, False
    if m in ('b', 'bl', 'blx', 'bx'):
        return 3, False
    return 1, False


class M4:
    def __init__(self, elf):
        self.sym = symbols(elf)
        self.img = flash_image(elf)
        mu = self.mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        mu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        mu.mem_map(FLASH, 4 << 20)
        mu.mem_map(RAM, 2 << 20)
        mu.mem_map(IO, 1 << 20)
        mu.mem_write(FLASH, self.img)
        self.stop = FLASH + (4 << 20) - 0x100          # return address: emulation ends there
        mu.mem_write(self.stop, b'\x00\xbf' * 8)
        self.sp0 = RAM + (2 << 20) - 16
        mu.mem_write(self.sp0 - STACK, b'\xa5' * STACK)
        self.md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
        self.blocks = {}
        self.cyc = 0
        self.prev_end = None
        self.prev_cond = False
        self.prof = None
        mu.hook_add(UC_HOOK_BLOCK, self._block)
        fns = sorted((a & ~1, n) for n, a in self.sym.items() if FLASH <= a < FLASH + len(self.img))
        self.fn_addr = [a for a, _ in fns]
        self.fn_name = [n for _, n in fns]

    def _cost(self, addr, size):
        c = self.blocks.get(addr)
        if c is None:
            cyc, prevmem, last = 0, False, None
            for ins in self.md.disasm(bytes(self.mu.mem_read(addr, size)), addr):
                k, prevmem = insn_cycles(ins.mnemonic, ins.op_str, prevmem)
                cyc += k
                last = ins
            c = self.blocks[addr] = (cyc, addr + size, last is not None and is_cond_branch(last.mnemonic))
        return c

    def _block(self, uc, addr, size, ud):
        cyc, end, cond = self._cost(addr, size)
        if self.prev_cond and addr != self.prev_end:
            cyc += 2                                     # taken conditional branch: pipeline refill
        self.cyc += cyc
        self.prev_end, self.prev_cond = end, cond
        if self.prof is not None:
            self.prof[self.fn_name[bisect.bisect_right(self.fn_addr, addr) - 1]] += cyc

    def call(self, fn, *args):
        mu = self.mu
        for r, a in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            mu.reg_write(r, a)
        mu.reg_write(UC_ARM_REG_SP, self.sp0)
        mu.reg_write(UC_ARM_REG_LR, self.stop | 1)
        self.cyc, self.prev_end, self.prev_cond = 0, None, False
        mu.emu_start(self.sym[fn] | 1, self.stop)
        return mu.reg_read(UC_ARM_REG_R0), self.cyc

    def u32(self, name):
        return int.from_bytes(self.mu.mem_read(self.sym[name], 4), 'little')

    def stack_used(self):
        st = bytes(self.mu.mem_read(self.sp0 - STACK, STACK))
        return STACK - (len(st) - len(st.lstrip(b'\xa5')))


class FlashModel:
    """STM32F4 ART accelerator, sampled: extra cycles for instruction and table reads from flash"""
    def __init__(self, m4):
        self.lo, self.hi = FLASH, FLASH + len(m4.img)
        self.ic, self.dc = collections.OrderedDict(), collections.OrderedDict()
        self.last = None
        self.cyc = 0
        m4.mu.hook_add(UC_HOOK_BLOCK, self._ifetch)
        m4.mu.hook_add(UC_HOOK_MEM_READ, self._read, begin=self.lo, end=self.hi)

    @staticmethod
    def _touch(cache, n, line):
        if line in cache:
            cache.move_to_end(line)
            return False
        cache[line] = 1
        if len(cache) > n:
            cache.popitem(last=False)
        return True

    def _ifetch(self, uc, addr, size, ud):
        for line in range(addr >> 4, ((addr + size - 1) >> 4) + 1):
            if self._touch(self.ic, 64, line) and (self.last is None or line != self.last + 1):
                self.cyc += WS
            self.last = line

    def _read(self, uc, acc, addr, size, val, ud):
        if self._touch(self.dc, 8, addr >> 4):
            self.cyc += WS


def load_pcm(path, rate):
    if path.endswith('.wav'):
        w = wave.open(path)
        assert w.getframerate() == rate and w.getnchannels() == 1 and w.getsampwidth() == 2, f'{rate} Hz mono 16-bit'
        return w.readframes(w.getnframes())
    return open(path, 'rb').read()


def mhz(c):
    return round(c * 50 / 1e6, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('elf')
    ap.add_argument('input')
    ap.add_argument('--frames', type=int, default=10 ** 9)
    ap.add_argument('--profile', action='store_true', help='encoder MHz by function')
    ap.add_argument('--flash', action='store_true', help='add the flash wait-state model (slow; use --frames 100)')
    ap.add_argument('--16k', dest='k16', action='store_true', help='v3.8: 16 kHz encoder and decoder')
    ap.add_argument('--bits')
    ap.add_argument('--pcm')
    ap.add_argument('--json')
    a = ap.parse_args()
    m4 = M4(a.elf)
    fl = FlashModel(m4) if a.flash else None
    sfx, fb = ('16', 640) if a.k16 else ('', 320)       # API suffix, input bytes a frame
    pcm = load_pcm(a.input, 16000 if a.k16 else 8000)
    n = min(len(pcm) // fb, a.frames)
    enc, _ = m4.call(f'sotto_enc{sfx}_create')
    dec, _ = m4.call(f'sotto_dec{sfx}_create')
    assert enc and dec, 'heap too small'
    ec, dc, fc, bits, out_pcm = [], [], [], b'', []

    def decode(c=None):
        _, c = m4.call(f'sotto_decode{sfx}', dec, IO + 0x1000, 1, IO + 0x2000)
        out_pcm.append(bytes(m4.mu.mem_read(IO + 0x2000, fb)))
        return c
    if a.profile:
        m4.prof = collections.Counter()
    for i in range(n):
        m4.mu.mem_write(IO, pcm[fb * i:fb * (i + 1)])
        f0 = fl.cyc if fl else 0
        r, c = m4.call(f'sotto_encode{sfx}', enc, IO, IO + 0x1000)
        ec.append(c)
        if fl:
            fc.append(fl.cyc - f0)
        if r:
            bits += bytes(m4.mu.mem_read(IO + 0x1000, 6))
            prof, m4.prof = m4.prof, None               # profile the encoder only
            dc.append(decode())
            m4.prof = prof
    m4.prof = None
    while True:
        r, _ = m4.call(f'sotto_enc{sfx}_flush', enc, IO + 0x1000)
        if not r:
            break
        bits += bytes(m4.mu.mem_read(IO + 0x1000, 6))
        decode()
    if a.bits:
        open(a.bits, 'wb').write(bits)
    if a.pcm:                                           # as sotto_dec_fx [-16] writes it
        skip = 1
        if a.k16:                                       # 5 frames in v3.8, 6 in v3.9
            skip = m4.call('sotto_dec16_delay_frames')[0] if 'sotto_dec16_delay_frames' in m4.sym else 5
        y = b''.join(out_pcm[skip:])
        while out_pcm:
            r, _ = m4.call(f'sotto_dec{sfx}_flush', dec, IO + 0x2000)
            if a.k16 and not r:
                break
            y += bytes(m4.mu.mem_read(IO + 0x2000, fb))
            if not a.k16:
                break
        open(a.pcm, 'wb').write(y)
    out = {
        'elf': os.path.relpath(a.elf), 'input': os.path.basename(a.input), 'frames': n,
        'encoder_MHz': {'mean': mhz(sum(ec) / len(ec)), 'worst': mhz(max(ec))},
        'decoder_MHz': {'mean': mhz(sum(dc) / len(dc)), 'worst': mhz(max(dc))},
        'heap_bytes': m4.u32('heap_used'), 'stack_bytes': m4.stack_used(),
        'flash_bytes': len(m4.img),
    }
    if fl:
        out['encoder_flash_wait_MHz'] = {'mean': mhz(sum(fc) / len(fc)), 'worst': mhz(max(fc))}
    if a.profile:
        tot = sum(prof.values())
        out['encoder_profile_MHz'] = {k: mhz(v / n) for k, v in prof.most_common(20)}
        out['encoder_profile_total_MHz'] = mhz(tot / n)
    js = json.dumps(out, indent=1)
    print(js)
    if a.json:
        open(a.json, 'w').write(js + '\n')


if __name__ == '__main__':
    sys.exit(main())
