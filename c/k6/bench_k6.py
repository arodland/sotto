"""Cycle estimate of the K6 voice pager's receive path on the KD32F328 (Cortex-M0, 96 MHz, two
flash wait states), with the Cortex-M0 bench's emulator and timing model (c/fixed/cm0/run.py).

Usage: python c/k6/bench_k6.py c/k6/build/bench.elf IN.raw|IN.wav|IN.flac [--ws N] [--lose 3,17,...]
       [--store BYTES] [--pcm OUT.raw] [--json OUT.json]
       python c/k6/bench_k6.py c/k6/build-lr/bench.elf IN --codec 1200   (K6_CODEC=1200 build:
       sotto 1200, magic 0xA9, played in 20 ms halves)
       python c/k6/bench_k6.py c/k6/build-vlr/bench.elf IN --codec 600   (K6_CODEC=vlr build:
       sotto 600 / 400, magic 0xAC / 0xAD, 4 / 6 calls of 20 ms per slot)
       python c/k6/bench_k6.py c/k6/build-vlrh-cbs/bench.elf IN --codec 600 --hiss   (K6_HISS=1
       build: the bits from the v3.8 hiss encoder on 16 kHz input, 320 PWM samples a frame)

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


def load_8k(path, rate=8000):
    """8 kHz (or rate) mono 16-bit samples: .raw as is, anything else through ffmpeg"""
    if path.endswith('.raw'):
        return open(path, 'rb').read()
    return subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', str(rate), '-f', 's16le', '-'],
                          check=True, capture_output=True).stdout


def encode(path, codec='2400', hiss=False):
    pcm = load_8k(path, 16000 if hiss else 8000)
    with tempfile.TemporaryDirectory() as d:
        open(f'{d}/in.raw', 'wb').write(pcm)
        # sotto 1200 has no fixed-point encoder: the float one (make -C c VARIANT=v3.7 sotto_enc);
        # the hiss band's bits come from the v3.8 float encoder's -16 mode (make -C c, 16 kHz in)
        enc = [os.path.join(ROOT, 'c', 'sotto_enc'), '-' + codec] if codec in ('1200', '600', '400') else \
            [os.path.join(ROOT, 'c', 'fixed', 'sotto_enc_fx')]
        if hiss:
            enc = [os.path.join(ROOT, 'c', 'sotto_enc'), '-16'] + (['-' + codec] if codec != '2400' else [])
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
    ap.add_argument('--codec', choices=('2400', '1200', '600', '400'), default='2400')
    ap.add_argument('--hiss', action='store_true', help='a K6_HISS=1 build: hiss band bits, 320 samples a frame')
    ap.add_argument('--payload', choices=('L', 'XL'), default='L', help='packet profile (not for 2400)')
    ap.add_argument('--yield-gap', action='store_true',
                    help='with the decoder yield hook set to sp_yield_probe (builds with FX_YIELD_HOOK): '
                         'the longest stretch of work between yields, in us at 96 MHz')
    ap.add_argument('--packet-yield-gap', action='store_true',
                    help='with sp_packet\'s yield set to sp_yield_probe (K6_CB=spi builds, SP_PKT_YIELD) and '
                         'stream mode on, as the coded 4FSK session runs it: the longest stretch of the '
                         'store\'s work between yields, in us at 96 MHz')
    a = ap.parse_args()
    cm0.WS = a.ws
    m = cm0.M0(a.elf)
    io = cm4.IO
    store, pkt, pwm = io + 0x10000, io + 0x100, io + 0x2000
    frames = encode(a.input, a.codec, a.hiss)
    seg = 640 if a.hiss else 320                  # bytes of PWM per 20 ms frame
    lose = {int(x) for x in a.lose.split(',') if x}
    if a.codec != '2400':
        sys.path.insert(0, ROOT)
        from modem import pager
        all_pk = pager.profile(a.payload, a.codec).pack(b''.join(frames), msg_id=0x21)
    else:
        all_pk = ref.pack_message(frames, 0x21)
    packets = [p for i, p in enumerate(all_pk) if i not in lose]
    if 'sp_cb_setup' in m.sym:   # codebook in SPI flash (K6_CB=...): check the stand-in flash
        r, c = m.call('sp_cb_setup')
        assert r > 0, r
        print(f'codebook check: {c / 96e3:.1f} ms')
    if 'sp_vlr_setup' in m.sym and a.codec in ('600', '400'):
        r, c = m.call('sp_vlr_setup')
        assert r > 0, r
        print(f'sotto 600 / 400 tables check: {c / 96e3:.1f} ms')
    nmax, _ = m.call('sp_init', store, a.store)
    pc, pgaps, plast = [], [], [0]
    if a.packet_yield_gap:
        from unicorn import UC_HOOK_CODE
        assert 'sp_set_packet_yield' in m.sym, 'build with K6_CB=spi (SP_PKT_YIELD)'
        probe = m.sym['sp_yield_probe'] & ~1

        def on_pk_yield(uc, addr, size, ud):
            pgaps.append(m.cyc - plast[0])
            plast[0] = m.cyc
        hp = m.mu.hook_add(UC_HOOK_CODE, on_pk_yield, begin=probe, end=probe + 1)
        m.call('sp_set_stream', 1)
        m.call('sp_set_packet_yield', m.sym['sp_yield_probe'] | 1, 0)
    for p in packets:
        m.mu.mem_write(pkt, p)
        plast[0] = 0                              # call() counts cycles from 0
        r, c = m.call('sp_packet', pkt, len(p))
        assert r >= 0, r
        pc.append(c)
        if a.packet_yield_gap:
            pgaps.append(m.cyc - plast[0])        # the last yield to the end of the call
    if a.packet_yield_gap:
        m.call('sp_set_packet_yield', 0, 0)
        m.mu.hook_del(hp)
    m.call('sp_play_start')
    fc, out, gaps = [], b'', []
    if a.yield_gap:
        from unicorn import UC_HOOK_CODE
        probe = m.sym['sp_yield_probe'] & ~1
        last = [0]

        def on_yield(uc, addr, size, ud):
            gaps.append(m.cyc - last[0])
            last[0] = m.cyc
        m.mu.hook_add(UC_HOOK_CODE, on_yield, begin=probe, end=probe + 1)
        m.call('sp_set_yield', m.sym['sp_yield_probe'] | 1)
    while True:
        if a.yield_gap:
            last[0] = 0                           # call() counts cycles from 0
        r, c = m.call('sp_play_pwm', pwm, 2000, a.gain, 1)
        if a.yield_gap:
            gaps.append(m.cyc - last[0])          # the last yield to the end of the call
        if not r:
            break
        fc.append(c)
        out += bytes(m.mu.mem_read(pwm, seg))
    if a.pcm:
        open(a.pcm, 'wb').write(out)
    res = {
        'input': os.path.basename(a.input), 'codec': a.codec, 'hiss': a.hiss, 'frames': len(frames), 'packets': len(packets),
        'lost_packets': sorted(lose), 'store_frames': nmax, 'flash_ws': a.ws,
        'play_MHz': {'mean': cm4.mhz(sum(fc) / len(fc)), 'worst': cm4.mhz(max(fc)), 'worst_frame': fc.index(max(fc)),
                     'p99': cm4.mhz(sorted(fc)[len(fc) * 99 // 100])},
        'packet_cycles': {'mean': round(sum(pc) / len(pc)), 'worst': max(pc)},
        'stack_bytes': m.stack_used(), 'flash_bytes': len(m.img),
        'static_ram_bytes': m.call('sp_static_bytes')[0],
    }
    if a.packet_yield_gap:
        pgaps.sort()
        res['packet_yield_gap_us'] = {'max': round(pgaps[-1] / 96, 1),
                                      'p99': round(pgaps[len(pgaps) * 99 // 100] / 96, 1), 'yields': len(pgaps)}
    if a.yield_gap:
        gaps.sort()
        res['yield_gap_us'] = {'max': round(gaps[-1] / 96, 1), 'p99': round(gaps[len(gaps) * 99 // 100] / 96, 1),
                               'yields': len(gaps)}
    js = json.dumps(res, indent=1)
    print(js)
    if a.json:
        open(a.json, 'w').write(js + '\n')


if __name__ == '__main__':
    main()
