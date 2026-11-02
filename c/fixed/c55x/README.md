# sotto on TI's C55x simulator

These files build the fixed-point sotto (`c/fixed/`) with TI's C5500 compiler and run it on
TI's cycle-accurate C55x simulator. They count cycles per frame and memory, and they check
that the DSP output is bit-identical to the x86 build. Results are in `docs/c-port.md`.

| File | What it is |
|---|---|
| `bench.c` | Encodes `in.raw` (16-bit, 8 kHz, up to 30 s) and writes `out.bit`, then decodes it and writes `out.raw`, using CIO file I/O on the host. Prints the cycles per frame (mean, minimum, maximum), the state sizes and the stack high-water marks. Build it with `-DFX_PROFILE` for a breakdown by block (the `FX_PROF` marks in the codec), with `-DFFT_BENCH` for the 512-point FFT alone, and with `-DBENCH16` to run 16 kHz audio through the v3.8 layer (`out.raw` is then 16 kHz, 5 frames later than `sotto_dec -16`'s output, which removes the layer's delay). |
| `cb_dots.asm`, `hsum.asm`, `nlp_dft.asm` | Hand-written kernels for the encoder's three hottest exact loops: the codebook dot products (`fx_cb_dots`, used with the unpacked codebook), the harmonic sums of the pitch refinement (`fx_hsum`) and the pitch estimator's 64-point DFT (`fx_nlp_dft`, one dual multiply-accumulate per cosine and sine pair). Each gives the same integers as its C version in `sotto_fx_enc.c`, which the x86 build uses; `-DFX_CB_DOTS_C`, `-DFX_HSUM_C` or `-DFX_NLP_DFT_C` uses the C version on the DSP too. |
| `recip48.asm`, `bins.asm`, `hfit.asm`, `divq15.asm`, `fft.asm`, `cb_resid.asm` | The second round's encoder kernels: the 2^48 reciprocal of the pitch refinement (`fx_recip48`: table seed, Newton step, exact remainder), the spectrum's bin loops (largest bin, 16-bit copy, powers), the voicing fit's three loops (`fx_hfit_sums` with the dual multiply-accumulate, `fx_hfit_err`, `fx_hfit_pow`) and its two divisions (`fx_div_q15_2`, 32 conditional subtractions each), the FFT's two butterfly loops (`fx_fft_bfly1`, `fx_fft_bfly_grp`) and the codebook search's residual (`fx_cb_resid`). The C versions are in `fx_math.c` (`-DFX_BINS_C`, `-DFX_HFIT_C`, `-DFX_FFT_C`, `-DFX_CB_RESID_C`) and `sotto_fx_enc.c` (`-DFX_RECIP_C`, `-DFX_DIVQ_C`). |
| `oa_pair.asm`, `mp_sums.asm`, `fft5.asm` | The decoder's overlap-add sums two samples at a time, with the cosine table as a circular buffer (`fx_oa_pair`); the minimum-phase dot products with the dual multiply-accumulate (`fx_mp_sums`); and the radix-5 stage of the v3.8 layer's 160-point FFT (`fx_fft5_stage32`). C versions: `fx_math.c` (`-DFX_OA_C`, `-DFX_MP_C`) and the loop in `sotto_fx_hiss.c` (`-DFX_FFT5_C`). |
| `kernels.c` | Checks every kernel against its C version on random inputs (and the edge cases: zero numerators, saturation, the last bin without a pointer step): `C55X_MAIN=kernels sh tools/c55x_build.sh`, then run `kernels.out` with `bench.js`. Prints `ok` for each. |
| `fxtest.c` | Checksums of the arithmetic primitives in `fx.h` and `fx_math.c`. Its output must match the same file built with gcc. |
| `bench.cmd` | Linker memory map: 32K words of dual-access RAM (DARAM) and 128K words of single-access RAM (SARAM), as on the C5517. SARAM is split into ranges that each stay inside one 64K-word page: the simulator stops with "PAGE BOUNDARY CROSSING" when an address register steps across a page end, even just past the last element of an array. The cosine table and the strings go in DARAM. |
| `bench16.cmd` | The same for `-DBENCH16`: v3.8's states don't fit DARAM, so the heap gets a 64K-word page of SARAM. `c55x_build.sh` picks it. |
| `bench.js` | Debug Server Scripting (DSS) script: loads the program, enables the profile clock that `clock()` reads, and runs to exit. |
| `tisim_c55x30_ca.ccxml` | Target configuration: C55xx Rev 3.0 CPU cycle-accurate simulator. |
| `../../../tools/c55x_build.sh` | Compiles and links (`-O3`, large memory model, `-vcpu:3.3`). |

## Running

```sh
make -C c fixed                                  # generates c/fixed/fx_tables.c
sh tools/c55x_build.sh                           # c/fixed/c55x/build/bench.out (v3.8 core, 8 kHz bench)
sh tools/c55x_build.sh -DBENCH16                 # 16 kHz in.raw through the v3.8 layer
cd c/fixed/c55x/build
head -c 16000 ../../../../codec/codec2/raw/ve9qrp_10s.raw > in.raw    # 1 s
/opt/ti/dss.sh 'Z:\path\to\sotto\c\fixed\c55x\bench.js' \
               'Z:\path\to\sotto\c\fixed\c55x\tisim_c55x30_ca.ccxml' \
               'Z:\path\to\sotto\c\fixed\c55x\build\bench.out'
../../sotto_enc_fx in.raw x86.bit && cmp out.bit x86.bit   # must be identical
```

The simulator runs about 1 s of audio in 1.5 minutes. `C55X_OUT=dir` builds into another
folder. The build stores the codebook one value per word (`-DFX_CB_UNPACKED`, 29 KB more
than packed, and needed by `cb_dots.asm`); `C55X_PACKED=1` builds the packed codebook and
its C search instead. `out.raw` is the x86 decoder's output (`c/fixed/sotto_dec_fx out.bit`) delayed by 160
samples (the command-line tool drops the first 20 ms), and otherwise identical.

## Getting the tools (Linux x86-64, 2026-11)

TI's downloads are free, but nothing from TI is committed here. The cloud environment needs
`software-dl.ti.com` and `downloads.ti.com` in its allowed hosts.

1. **Compiler, C5500 CGT 4.4.1.** The download page wants a login, but TI's p2 update site
   serves the same installer:
   `https://software-dl.ti.com/dsps/dsps_public_sw/sdo_ccstudio/codegen/Updates/p2linux/binary/com.ti.cgt.c5500.4.4.linux_root_4.4.1`
   is a zip holding `downloads/ti_cgt_c5500_4.4.1_linux_installer_x86.bin`. The installer is
   32-bit: `dpkg --add-architecture i386 && apt-get update && apt-get install libc6:i386
   libstdc++6:i386`, then `./ti_cgt_c5500_4.4.1_linux_installer_x86.bin --mode unattended
   --prefix /opt/ti/c5500_4.4.1`. `tools/c55x_build.sh` looks for it there (or set
   `C55X_CGT`).
2. **Simulator.** The Linux versions of Code Composer Studio have no C55x simulator. The
   Windows CCS 5.5 has one and runs headless under wine:
   - `apt-get install wine wine32:i386 xvfb`, with `WINEPREFIX=/opt/wine32 WINEARCH=win32`.
   - Download `https://software-dl.ti.com/dsps/dsps_public_sw/sdo_ccstudio/CCSv5/CCS_5_5_0/exports/CCS5.5.0.00077_win32.zip`
     (1.3 GB). Only a few packages inside it are needed, each a zip under
     `featurerepo/binary/` or `baserepo/` with an installer in its `downloads/` folder:
     the debug server (`com.ti.ccstudio.debugserver.win32_root_5.5.0.400`, installed with
     `--mode unattended --installdir 'C:\ti\ccsv5'`), the simulators
     (`com.ti.sim.pack.setup.win32_root_5.5.0`, `--mode silent --prefix
     'C:\ti\ccsv5\ccs_base'`), the emulation pack
     (`com.ti.emulation.pack.win32_root_5.1.232.0`, for `tpi55xx.dll`), the Java runtime
     (`baserepo/jre_1.6.0_13.zip`, unzipped to `C:\ti\ccsv5\ccs_base\jre`) and
     `baserepo/vcredist_x86.exe /q` (the Visual C++ 2008 runtime that `tpi55xx.dll` needs).
     Run each installer with `xvfb-run -a wine`.
   - The emulation pack's installer doesn't finish under wine. What it copied is enough
     after two fixes: copy `ccs_base/ccs_base/*` up one level, and add the line
     `"TMS320C55XX"="..\..\..\emulation\tpi\tpi55xx"` to `emulation/tpi/tpi_ds.ini`; also
     link `C:\ti\ccsv5\emulation` to `ccs_base\emulation`.
   - `/opt/ti/dss.sh` runs a DSS script with that Java:

     ```sh
     #!/bin/sh
     export WINEPREFIX=/opt/wine32 WINEARCH=win32 WINEDEBUG=-all
     D='C:\ti\ccsv5\ccs_base\DebugServer\packages\ti\dss\java'
     exec wine 'C:\ti\ccsv5\ccs_base\jre\bin\java.exe' -Xms40m -Xmx384m \
       -cp "$D\\js.jar;$D\\dss.jar;C:\\ti\\ccsv5\\ccs_base\\dvt\\scripting\\dvt_scripting.jar" \
       org.mozilla.javascript.tools.shell.Main "$@"
     ```

## C55x rules the code follows

`c/fixed/fx.h` lists them. In short: `int` and `char` are 16 bits, `long long` is 40 bits
(so `fx_acc` is the accumulator type), C89 declarations, and no 32 × 32-bit products typed
`long long` in loops (each is a library call). With this compiler, splitting a 32-bit value
into an unsigned 16-bit low half miscompiled in a loop, so the split is signed 15-bit
(`FX_HI`, `FX_LO`).
