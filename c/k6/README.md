# c/k6: the K6 voice pager's receive library

Packet reassembly and playback for receiving sotto messages on a Baofeng UV-K6 (KD32F328,
Cortex-M0). The design and measurements are in [`docs/k6-pager.md`](../../docs/k6-pager.md), and
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

SPDX-License-Identifier: MIT
