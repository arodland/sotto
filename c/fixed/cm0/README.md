# Cortex-M0 bench (Baofeng UV-K6, Quansheng UV-K5 class)

The Cortex-M4 bench (`../cm4/`) with the Cortex-M0's timings: a bare-metal ARMv6-M build of
the fixed-point sotto run frame by frame in an instruction emulator.

```sh
pip install unicorn capstone
sh tools/cm0_build.sh                    # c/fixed/cm0/build/bench.elf (clang + lld, -O2)
python c/fixed/cm0/run.py c/fixed/cm0/build/bench.elf in.raw --bits m0.bit --pcm m0.raw
make -C c VARIANT=v3.7 fixed && c/fixed/sotto_enc_fx in.raw x86.bit && cmp m0.bit x86.bit
```

`--ws N` sets the flash wait states (default 2, the KD32F328 at 96 MHz; `--ws 0` for code in
RAM), `--profile` gives the encoder's MHz by function. `rt.c` holds the run-time helpers
ARMv6-M lacks instructions for (32-bit division, 64-bit multiply and shifts, CLZ), written with
32-bit operations as libgcc's are; `divmod.s` is the ARMv6-M form of the 64-bit division entry
points. The timing model is in `run.py`'s docstring. Results: `docs/ht-survey.md`.

SPDX-License-Identifier: MIT
