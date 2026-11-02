# sotto decoder in WebAssembly

The listening page (`docs/index.html`, https://baitisj.github.io/sotto/) decodes its sotto v3.8
rows in the browser. This folder builds that decoder: the same C code as `libsotto` (`../sotto.c`,
`../sotto_hiss.c` and the Codec 2 synthesis it calls), compiled to `wasm32-wasi` with one small
entry file (`sotto_wasm.c`) that decodes a whole received stream at a time.

```sh
sudo apt install clang lld wasi-libc libclang-rt-18-dev-wasm32   # Debian/Ubuntu names
make                     # in the repository root first: codebook, hiss tables, Codec 2 build
make -C c/wasm install   # builds sotto_dec.wasm (about 200 KB) and copies it to docs/
make -C c/wasm check     # node: decodes docs/samples/v38/*.bit, compares with c/sotto_dec -16
```

`WASI_SYSROOT=/path/to/wasi-sysroot` points the build at a sysroot clang doesn't find by itself
(for example from wasi-sdk).

`docs/sotto-dec.js` loads the module (`loadSotto(url)`) and gives `decode16(frames, good)`:
`frames` holds 6-byte frames, `good` (optional) one byte per frame, 0 where the frame failed its
CRC. It returns 16 kHz 16-bit samples aligned with the encoder input, the same samples as
`c/sotto_dec -16 IN.bit OUT.raw GOOD`. The module needs no files: the three WASI imports it
links (stdio) are stubbed out in the loader.

The page's bitstreams and scores come from `tools/render_page_v38.py`.
