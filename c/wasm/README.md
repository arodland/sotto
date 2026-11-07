# sotto encoder and decoder in WebAssembly

The listening page (`docs/index.html`, https://baitisj.github.io/sotto/) decodes its sotto rows
in the browser, and its "Try it with your own voice" section records the microphone (or reads an
audio file), encodes it at all four rates (2400, 1200, 600 and 400 bit/s) and decodes each one
again. This folder builds those codecs from the same C code as `libsotto`, compiled to
`wasm32-wasi` with one small entry file (`sotto_wasm.c`) that encodes or decodes a whole stream
at a time:

| module | code | rates |
|---|---|---|
| `sotto.wasm` (float, v3.8) | `../sotto.c`, `../sotto_hiss.c`, `../sotto_lr.c`, `../sotto_vlr.c`, `../sotto_vlr_dec.c` and the parts of Codec 2 they call | 2400 (v3.8), 1200 hiss, 600 hiss and 400 hiss, encode and decode at 16 kHz; plain 600 / 400 encode (8 kHz in) |
| `sotto_vlr.wasm` (fixed point, v3.7 core) | `../fixed/sotto_fx_vlr.c`, `../fixed/sotto_fx_dec.c` | plain 600 / 400 decode, 8 kHz out: the decoder the K6 pager runs |

The page's four rates all carry v3.8's 4-8 kHz hiss band. Plain sotto 600 / 400 (no hiss band,
what the K6 sends and plays) decode on the v3.7 core (no v3.8 hiss rules), so their decoder is a
separate module built without `-DSOTTO_V38`, as `make VARIANT=v3.7 fixed` builds
`sotto_vlr_dec_fx`; the page uses it only for the "600 plain (K6)" and "400 plain (K6)" entries
of its .sot menu.

```sh
sudo apt install clang lld wasi-libc libclang-rt-18-dev-wasm32   # Debian/Ubuntu names
make                     # in the repository root first: the Codec 2 build (generated codebooks)
make -C c/wasm install   # builds sotto.wasm (about 310 KB) and sotto_vlr.wasm (60 KB), copies them to docs/
make -C c/wasm check     # node: compares every decoder and encoder with the native tools on the
                         # page clips (c/sotto_enc, c/sotto_dec, and a VARIANT=v3.7
                         # sotto_vlr_dec_fx it builds in build/c37; encoder part needs python3
                         # with numpy, scipy and soundfile)
```

`WASI_SYSROOT=/path/to/wasi-sysroot` points the build at a sysroot clang doesn't find by itself
(for example from wasi-sdk).

`make -C c/wasm sotto_fx.wasm` builds the fixed-point codec (`c/fixed`, the integer code that
runs on the C5517) behind the same entry points, for comparing fixed-point versions in the
browser. Being integer only, it gives exactly the bits of `c/fixed/sotto_enc_fx -16` (apart
from the padded last frame). It isn't installed into `docs/`.

`docs/sotto.js` loads the modules (`loadSotto(url, vlrUrl)`; `vlrUrl` is optional) and gives
`encode(rate, speech)` and `decode(rate, frames, good)` for rate 2400, 1200, 600 or 400 (all with
the hiss band), plus the 2400-only `encode16` and `decode16` below. `encode` takes 16 kHz speech
and `decode` returns `{ pcm, sampleRate: 16000 }`. Every rate's frame is 6 bytes (48 bits)
covering 20, 40, 80 or 120 ms. The outputs match `sotto_enc -16 [-1200|-600|-400]` /
`sotto_dec -16 [-1200|-600|-400]` (every frame on the page clips; decoded samples within 1 LSB).
For plain sotto 600 / 400, `encodePlain(rate, speech16)` first halves the rate in C (`down2`, the
same filter and rounding as scipy's `resample_poly(x, 1, 2)`) and gives the bytes of
`sotto_enc -600|-400` of that 8 kHz input; `decodePlain(rate, frames, good)` (needs
`sotto_vlr.wasm`) gives 8 kHz, the samples of `sotto_vlr_dec_fx 600|400` (bit-exact: integer
only). The plain decoder plays a hiss stream too, without its hiss band (and with a wrong pitch
slope where only one frame of a superframe is pitched), which is how the check exercises it. Each call starts Codec 2's random generator afresh, as a new
process does, so the result doesn't depend on what the page encoded before.

`encode16` takes an `Int16Array` of 16 kHz speech and returns a `Uint8Array` of 6-byte frames, the
same bytes as `c/sotto_enc -16 IN.raw OUT.bit`, except that a last partial 20 ms frame is padded
with silence and encoded (`sotto_enc` drops it). The encoder works best with speech at -26 dBFS;
`levelTo(float32, -26)` scales float samples to that and returns `{ pcm, gainDb }`. Every frame
matches the native encoder on the ten page clips. (The two libms may differ in the last bit of a
few results, which could flip a near-tie, so the check allows 3% of frames to differ. The 98-100%
measured before came from Codec 2's random generator carrying over from the page's earlier
decodes; each call now restarts it.)

The page saves these frames as `.sot` files: raw frames, 6 bytes each (300, 150, 75 or 50 bytes a
second), no header, so the native tools decode them and the page's "Play a .sot file" button reads
their output. The page names each download after its rate (`sotto-…-600.sot`) and takes the rate
from that name, or else from its menu.

`decode16`:
`frames` holds 6-byte frames, `good` (optional) one byte per frame, 0 where the frame failed its
CRC. It returns 16 kHz 16-bit samples aligned with the encoder input, the same samples as
`c/sotto_dec -16 IN.bit OUT.raw GOOD`. The module needs no files: the three WASI imports it
links (stdio) are stubbed out in the loader.

`wavBlob(pcm, rate)` wraps samples as a 16-bit WAV for an `<audio>` element or a download.

The page's bitstreams and scores come from `tools/render_page_v38.py` (v3.8 over the radio
channel) and `tools/render_page_rates.py` (the four rates, `docs/samples/rates/`).
