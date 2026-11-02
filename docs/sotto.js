// sotto v3.8 decoder in the browser: loads sotto.wasm (c/wasm, built from the C port in c/)
// and turns received frames into 16 kHz speech. Used by index.html; c/wasm/check_wasm.mjs runs
// the same module under node to compare it with the native decoder.
// SPDX-License-Identifier: MIT

// The decoder never touches files; these stubs satisfy the few WASI imports the C library
// links (stdio), and report failure if anything calls them.
const wasiStubs = {
  fd_write: () => 52, fd_seek: () => 52, fd_close: () => 52,
};

export async function loadSotto(source) {
  const bytes = source instanceof ArrayBuffer || ArrayBuffer.isView(source)
    ? source : await (await fetch(source)).arrayBuffer();
  const { instance } = await WebAssembly.instantiate(bytes, { wasi_snapshot_preview1: wasiStubs });
  const x = instance.exports;
  x._initialize();
  const cstr = p => {
    const m = new Uint8Array(x.memory.buffer);
    let e = p; while (m[e]) e++;
    return new TextDecoder().decode(m.subarray(p, e));
  };
  return {
    format: cstr(x.sotto_wasm_format()),
    sampleRate: 16000,
    // frames: Uint8Array of 6-byte frames; good: optional Uint8Array, one byte per frame,
    // 0 where the frame failed its CRC. Returns Int16Array speech aligned with the encoder input.
    decode16(frames, good) {
      const n = Math.floor(frames.length / 6);
      const nOut = x.sotto_wasm_out_len16(n);
      const pb = x.sotto_wasm_alloc(6 * n), pg = good ? x.sotto_wasm_alloc(n) : 0;
      const po = x.sotto_wasm_alloc(2 * nOut);
      try {
        new Uint8Array(x.memory.buffer, pb, 6 * n).set(frames.subarray(0, 6 * n));
        if (good) new Uint8Array(x.memory.buffer, pg, n).set(good.subarray(0, n));
        const w = x.sotto_wasm_decode16(pb, pg, n, po);
        if (w < 0) throw new Error("sotto: decoder allocation failed");
        return new Int16Array(x.memory.buffer, po, w).slice();
      } finally {
        x.sotto_wasm_free(pb); if (pg) x.sotto_wasm_free(pg); x.sotto_wasm_free(po);
      }
    },
  };
}

// 16-bit mono WAV, for an <audio> element (blob URL) or a download.
export function wavBlob(pcm, rate) {
  const b = new ArrayBuffer(44 + 2 * pcm.length), v = new DataView(b);
  const s = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  s(0, "RIFF"); v.setUint32(4, 36 + 2 * pcm.length, true); s(8, "WAVEfmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, 2 * rate, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); s(36, "data"); v.setUint32(40, 2 * pcm.length, true);
  new Int16Array(b, 44).set(pcm);
  return new Blob([b], { type: "audio/wav" });
}
