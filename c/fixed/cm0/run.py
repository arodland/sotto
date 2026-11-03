"""Cycle estimate for the fixed-point sotto on a Cortex-M0 (Baofeng UV-K6 "K6 Plus Open": KD32F328,
96 MHz; Quansheng UV-K5/K6: DP32G030, 48 MHz), from the Cortex-M4 bench's emulator with the
Cortex-M0's timings and a flash wait-state model.

Usage: python c/fixed/cm0/run.py BUILD/bench.elf IN.raw|IN.wav [--16k] [--frames N] [--ws N] [--profile]
       [--bits OUT.bit] [--pcm OUT.raw] [--json OUT.json]

Build with tools/cm0_build.sh. Everything else (driving sotto_encode / sotto_decode a frame at
a time, --bits, --pcm, heap and stack) is c/fixed/cm4/run.py; this file swaps in:

Timing model (ARM Cortex-M0 TRM, section 3.3), zero wait states:
  data processing and MULS (the single-cycle multiplier both chips have): 1
  LDR/STR (any width): 2; LDM/STM/PUSH/POP: 1 + registers (POP with pc: 4 + registers)
  B and taken conditional branches: 3, not taken 1; BL: 4; BX/BLX: 3
ARMv6-M has no 64-bit multiply, no divide and no CLZ, so those are calls into cm0/rt.c and are
costed instruction by instruction like the codec itself.
Flash (--ws, default 2 = KD32F328 at 96 MHz, LATENCY=2; 0 = code in RAM or HCLK <= 24 MHz): an
instruction word (two Thumb instructions) costs 1 + ws cycles to fetch and the prefetch buffer
overlaps that with execution, so a basic block costs max(core cycles, fetch cycles); a taken
branch adds ws (the refill can't be prefetched) and every data read from flash (tables and
literal pools) adds ws. Treat it as +-25 %.
Needs: pip install unicorn capstone.  SPDX-License-Identifier: MIT
"""
import importlib.util, os, sys
_spec = importlib.util.spec_from_file_location(
    'cm4_run', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cm4', 'run.py'))
cm4 = importlib.util.module_from_spec(_spec)            # c/fixed/cm4/run.py
_spec.loader.exec_module(cm4)
from unicorn import UC_HOOK_MEM_READ                    # noqa: E402
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M0      # noqa: E402

WS = 2


def insn_cycles(m, op, prevmem):
    m = m.split('.')[0]
    if m.startswith('ldm') or m.startswith('stm') or m == 'push':
        return 1 + op.count(',') + 1, False
    if m == 'pop':
        return (4 if 'pc' in op else 1) + op.count(',') + 1, False
    if m.startswith('ldr') or m.startswith('str'):
        return 2, True
    if m == 'b':
        return 3, False
    if m == 'bl':
        return 4, False
    if m in ('bx', 'blx'):
        return 3, False
    if m in ('mov', 'add') and op.startswith('pc'):
        return 3, False
    return 1, False


class M0(cm4.M4):
    def __init__(self, elf):
        cm4.UC_CPU_ARM_CORTEX_M4 = UC_CPU_ARM_CORTEX_M0      # M4.__init__ sets this cpu model
        super().__init__(elf)
        self.mu.hook_add(UC_HOOK_MEM_READ, self._flash_read, begin=cm4.FLASH, end=cm4.FLASH + len(self.img))

    def _flash_read(self, uc, acc, addr, size, val, ud):
        self.cyc += WS

    def _cost(self, addr, size):
        c = self.blocks.get(addr)
        if c is None:
            core, last = 0, None
            for ins in self.md.disasm(bytes(self.mu.mem_read(addr, size)), addr):
                core += insn_cycles(ins.mnemonic, ins.op_str, False)[0]
                last = ins
            words = ((addr + size - 1) >> 2) - (addr >> 2) + 1
            cond = last is not None and cm4.is_cond_branch(last.mnemonic)
            c = self.blocks[addr] = (max(core, words * (1 + WS)) if WS else core, addr + size, cond)
        return c

    def _block(self, uc, addr, size, ud):
        cyc, end, cond = self._cost(addr, size)
        if self.prev_end is not None and addr != self.prev_end:
            cyc += WS + (2 if self.prev_cond else 0)     # taken branch: refill (+2 for a conditional)
        self.cyc += cyc
        self.prev_end, self.prev_cond = end, cond
        if self.prof is not None:
            self.prof[self.fn_name[cm4.bisect.bisect_right(self.fn_addr, addr) - 1]] += cyc


def main():
    global WS
    if '--ws' in sys.argv:
        i = sys.argv.index('--ws')
        WS = int(sys.argv[i + 1])
        del sys.argv[i:i + 2]
    cm4.M4 = M0                                          # cm4.main() builds the emulator by this name
    if '--flash' in sys.argv:
        sys.exit('--flash is the STM32F4 model; use --ws here')
    return cm4.main()


if __name__ == '__main__':
    sys.exit(main())
