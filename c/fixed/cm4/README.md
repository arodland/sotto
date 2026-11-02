# Cortex-M4 bench (TYT MD-UV380/390, Module17, CS7000)

A cycle estimate for the fixed-point sotto on the STM32F405 (Cortex-M4, 168 MHz) that the
OpenRTX radios use, without a board: a bare-metal build run one frame at a time in an
instruction emulator, every executed instruction costed with the Cortex-M4's published
timings.

```sh
pip install unicorn capstone             # the emulator and the disassembler
sh tools/cm4_build.sh                    # c/fixed/cm4/build/bench.elf (clang + lld, -O2)
python c/fixed/cm4/run.py c/fixed/cm4/build/bench.elf in.raw --bits m4.bit
make -C c fixed && c/fixed/sotto_enc_fx in.raw x86.bit && cmp m4.bit x86.bit
```

`in.raw` is 8 kHz 16-bit mono (or a .wav). The output is the encoder's and decoder's MHz for
real time (mean and worst frame), heap, stack and flash bytes. For v3.8 at 16 kHz:
`VARIANT=v3.8 sh tools/cm4_build.sh`, then `run.py ... in16k.raw --16k --bits m4.bit --pcm
m4.raw`, and compare with `sotto_enc_fx -16` and `sotto_dec_fx -16`. `--profile` adds the
encoder's MHz by function, and `--flash` adds the STM32F4's flash wait states (slow, so use
`--frames 100`). `sh tools/cm4_build.sh -Os` gives the size-optimised build that OpenRTX
uses by default.

Files: `shim.c` (bump allocator, memory functions, 64-bit division), `divmod.s` (the ABI's
64-bit division entry points), `inc/` (C library header stand-ins), `link.ld`, `run.py`
(the emulator, the timing model and the flash model, described in its docstring).

The code that runs on the M4 is the ordinary `c/fixed/` C. `FX_ARM_DSP` (fx.h) is on for any
ARM target with the DSP extension. It takes 32 x 32-bit products directly (`FX_WIDE_MUL`),
because the M4's SMULL and SMLAL take one cycle, searches an unpacked codebook with SMLAD,
and does the 16-bit dot products (resamplers, minimum phase), the overlap-add and the NLP
pitch DFT with SMLALD/SMLSLD, two products per instruction into 64 bits. Results and the method's limits are in `docs/c-port.md`, "Fixed point on the
Cortex-M4".

SPDX-License-Identifier: MIT
