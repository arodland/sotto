# c/k6: the K6 voice pager's receive library

Packet reassembly and playback for receiving sotto messages on a Baofeng UV-K6 (KD32F328,
Cortex-M0). The design and measurements are in [`docs/k6-receiver.md`](../../docs/k6-receiver.md), and
the firmware that links this library is in [`firmware/k6-aura/`](../../firmware/k6-aura/).

```sh
make -C c/k6                                    # pager_host: the same path on this machine
make -C c VARIANT=v3.7 fixed && python -m pytest tests/test_k6_pager.py
sh tools/k6_build.sh                            # build/libsotto_k6.a + build/bench.elf (clang, lld)
python c/k6/bench_k6.py c/k6/build/bench.elf clip.wav   # M0 cycles (pip install unicorn capstone)
c/k6/pager_host -t                              # the calibration test packet, hex
```

- `sotto_pager.h/.c` holds the library. It takes packets in the K6 voice-pager format, version 1
  (profiles L and S), and holds the message in the caller's RAM with per-frame good flags. It
  decodes with the fixed-point v3.7 decoder on its Cortex-M0 path, and converts to PWM values with
  pre-emphasis and automatic gain.
- `k6fsk_ref.py` is a copy of the packet spec's reference packer (`k6-fsk-packet-spec.md`, project
  files `ht-survey/k6fsk/`). The tests use it to make packets.
- `pager_host.c` is the host harness, and `bench_k6.py` is the emulator bench.
- `k6_mod.h/.c` is the RAM module loader: signed native modules sent as `0xAB` packets land in
  the message store and run once ([`docs/k6-modules.md`](../../docs/k6-modules.md)). `mod/hello.c`
  is the first module, `mod/module.ld` their linker script, and `km_host.c` the loader on this
  machine for `tests/test_k6_mod.py` (`make -C c/k6 km_host`, test key 00..0f).
- `cwid.h/.c` decodes a repeater's Morse ID from one of the FD6818's detectors (an audio level
  in dB, or a tone detector's key state): any speed, the detector's lag taken out, callsign
  check. `mod/cwprobe.c` is the API level 2 module that tried the detectors on the radio,
  `mod/cwlisten.c` the listener built on the one that won, the tone detector ([`docs/k6-cwid.md`](../../docs/k6-cwid.md), `tests/test_k6_cwid.py`).
- `sp_cb.h/.c` puts sotto's 30 KB codebook in the radio's SPI flash, read a vector at a time
  (`-DFX_CB_SPI`), with a power-on check, a one-time installer and a speed bench
  ([`docs/k6-spi-codebook.md`](../../docs/k6-spi-codebook.md)). `cb_image.c` writes the image,
  `flash_host.c` is the emulated NOR flash for `pager_host_cbs` / `_lr_cbs` / `_cbi`, and
  `flash_bench.c` is the stand-in for `bench.elf` (`tests/test_k6_cb.py`).

SPDX-License-Identifier: MIT
