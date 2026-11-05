"""Cycle estimate of the K6 voice pager's receive path on the KD32F328 (Cortex-M0, 96 MHz, two
flash wait states), with the Cortex-M0 bench's emulator and timing model (c/fixed/cm0/run.py).

Usage: python c/k6/bench_k6.py c/k6/build/bench.elf IN.raw|IN.wav|IN.flac [--ws N] [--lose 3,17,...]
       [--store BYTES] [--pcm OUT.raw] [--json OUT.json]
       python c/k6/bench_k6.py c/k6/build-lr/bench.elf IN --codec 1200   (K6_CODEC=1200 build:
       sotto 1200, magic 0xA9, played in 20 ms halves)

Encodes IN with c/fixed/sotto_enc_fx (v3.7; make -C c VARIANT=v3.7 fixed), packs the frames
into packets with c/k6/k6fsk_ref.py (profile L), drops the --lose packets, then on the
emulated M0: sp_init, sp_packet for each packet, sp_play_start, and sp_play_pwm (decode plus
PWM conversion) for every frame, as the radio runs them. Reports MHz per 20 ms frame (mean and
worst), the cycles per packet, and the stack; --pcm writes the PWM compare values the M0 produced
(uint16, top 2000). Build the ELF with tools/k6_build.sh. Needs: pip install unicorn capstone; ffmpeg
for inputs other than 8 kHz .raw.
SPDX-License-Identifier: MIT"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import k6fsk_ref as ref  # noqa: E402

_spec = importlib.util.spec_from_file_location('cm0_run', os.path.join(ROOT, 'c', 'fixed', 'cm0', 'run.py'))
cm0 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm0)
cm4 = cm0.cm4


def load_8k(path):
    """8 kHz mono 16-bit samples: .raw as is, anything else through ffmpeg"""
    if path.endswith('.raw'):
        return open(path, 'rb').read()
    return subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', '8000', '-f', 's16le', '-'],
                          check=True, capture_output=True).stdout


def encode(path, codec='2400'):
    pcm = load_8k(path)
    with tempfile.TemporaryDirectory() as d:
        open(f'{d}/in.raw', 'wb').write(pcm)
        # sotto 1200 has no fixed-point encoder: the float one (make -C c VARIANT=v3.7 sotto_enc)
        enc = [os.path.join(ROOT, 'c', 'sotto_enc'), '-1200'] if codec == '1200' else \
            [os.path.join(ROOT, 'c', 'fixed', 'sotto_enc_fx')]
        subprocess.run(enc + [f'{d}/in.raw', f'{d}/o.bit'], check=True)
        b = open(f'{d}/o.bit', 'rb').read()
    return [b[i:i + 6] for i in range(0, len(b) - 5, 6)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('elf')
    ap.add_argument('input')
    ap.add_argument('--ws', type=int, default=2)
    ap.add_argument('--lose', default='')
    ap.add_argument('--store', type=int, default=6144)
    ap.add_argument('--gain', type=int, default=256)
    ap.add_argument('--pcm')
    ap.add_argument('--json')
    ap.add_argument('--codec', choices=('2400', '1200'), default='2400')
    a = ap.parse_args()
    cm0.WS = a.ws
    m = cm0.M0(a.elf)
    io = cm4.IO
    store, pkt, pwm = io + 0x10000, io + 0x100, io + 0x2000
    frames = encode(a.input, a.codec)
    lose = {int(x) for x in a.lose.split(',') if x}
    if a.codec == '1200':
        sys.path.insert(0, ROOT)
        from modem import pager
        all_pk = pager.profile('L', '1200').pack(b''.join(frames), msg_id=0x21)
    else:
        all_pk = ref.pack_message(frames, 0x21)
    packets = [p for i, p in enumerate(all_pk) if i not in lose]
    nmax, _ = m.call('sp_init', store, a.store)
    pc = []
    for p in packets:
        m.mu.mem_write(pkt, p)
        r, c = m.call('sp_packet', pkt, len(p))
        assert r >= 0, r
        pc.append(c)
    m.call('sp_play_start')
    fc, out = [], b''
    while True:
        r, c = m.call('sp_play_pwm', pwm, 2000, a.gain, 1)
        if not r:
            break
        fc.append(c)
        out += bytes(m.mu.mem_read(pwm, 320))
    if a.pcm:
        open(a.pcm, 'wb').write(out)
    res = {
        'input': os.path.basename(a.input), 'codec': a.codec, 'frames': len(frames), 'packets': len(packets),
        'lost_packets': sorted(lose), 'store_frames': nmax, 'flash_ws': a.ws,
        'play_MHz': {'mean': cm4.mhz(sum(fc) / len(fc)), 'worst': cm4.mhz(max(fc))},
        'packet_cycles': {'mean': round(sum(pc) / len(pc)), 'worst': max(pc)},
        'stack_bytes': m.stack_used(), 'flash_bytes': len(m.img),
        'static_ram_bytes': m.call('sp_static_bytes')[0],
    }
    js = json.dumps(res, indent=1)
    print(js)
    if a.json:
        open(a.json, 'w').write(js + '\n')


if __name__ == '__main__':
    main()
