# sotto encoder and decoder in WebAssembly

The listening page (`docs/index.html`, https://baitisj.github.io/sotto/) decodes its sotto v3.8
rows in the browser, and its "Try it with your own voice" section records the microphone (or
reads an audio file), encodes it and decodes it again. This folder builds that codec: the same C
code as `libsotto` (`../sotto.c`, `../sotto_hiss.c` and the parts of Codec 2 they call), compiled
to `wasm32-wasi` with one small entry file (`sotto_wasm.c`) that encodes or decodes a whole stream
at a time.

```sh
sudo apt install clang lld wasi-libc libclang-rt-18-dev-wasm32   # Debian/Ubuntu names
make                     # in the repository root first: codebook, hiss tables, Codec 2 build
make -C c/wasm install   # builds sotto.wasm (about 200 KB) and copies it to docs/
make -C c/wasm check     # node: compares the decoder with c/sotto_dec -16 and the encoder
                         # with c/sotto_enc -16 on the page clips (encoder part needs
                         # python3 with numpy and soundfile)
```

`WASI_SYSROOT=/path/to/wasi-sysroot` points the build at a sysroot clang doesn't find by itself
(for example from wasi-sdk).

`docs/sotto.js` loads the module (`loadSotto(url)`) and gives `encode16(speech)` and
`decode16(frames, good)`.

`encode16` takes an `Int16Array` of 16 kHz speech and returns a `Uint8Array` of 6-byte frames, the
same bytes as `c/sotto_enc -16 IN.raw OUT.bit`, except that a last partial 20 ms frame is padded
with silence and encoded (`sotto_enc` drops it). The encoder works best with speech at -26 dBFS;
`levelTo(float32, -26)` scales float samples to that and returns `{ pcm, gainDb }`. The two libms
differ in the last bit of a few results, which can flip a near-tie in the pitch tracker or the VQ
search, so 98-100% of frames match the native encoder (measured on the ten page clips).

The page saves these frames as `.sot` files: raw frames, 6 bytes per 20 ms (300 bytes a second),
no header, so `c/sotto_dec -16 file.sot out.raw` decodes them and the page's "Play a .sot file"
button reads `sotto_enc -16` output.

`decode16`:
`frames` holds 6-byte frames, `good` (optional) one byte per frame, 0 where the frame failed its
CRC. It returns 16 kHz 16-bit samples aligned with the encoder input, the same samples as
`c/sotto_dec -16 IN.bit OUT.raw GOOD`. The module needs no files: the three WASI imports it
links (stdio) are stubbed out in the loader.

`wavBlob(pcm, rate)` wraps samples as a 16-bit WAV for an `<audio>` element or a download.

The page's bitstreams and scores come from `tools/render_page_v38.py`.
