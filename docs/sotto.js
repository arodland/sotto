// sotto in the browser: loads sotto.wasm (c/wasm, built from the C port in c/), which encodes
// 16 kHz speech into 6-byte frames and decodes frames back into speech, and optionally
// sotto_vlr.wasm, the fixed-point sotto 600 / 400 decoder. Every rate sends 48 bits per frame:
//   2400  v3.8, 20 ms frames, 16 kHz out
//   1200  sotto 1200 hiss, 40 ms superframes, 16 kHz out
//    600  sotto 600, 80 ms superframes, 8 kHz out
//    400  sotto 400, 120 ms superframes, 8 kHz out
// Used by index.html; c/wasm/check_wasm.mjs runs the same modules under node against the
// native tools.
// SPDX-License-Identifier: MIT

// The decoder never touches files; these stubs satisfy the few WASI imports the C library
// links (stdio), and report failure if anything calls them.
const wasiStubs = {
  fd_write: () => 52, fd_seek: () => 52, fd_close: () => 52,
};

export const RATES = {
  2400: { name: "sotto v3.8", frameMs: 20, sampleRate: 16000 },
  1200: { name: "sotto 1200 hiss", frameMs: 40, sampleRate: 16000 },
  600: { name: "sotto 600", frameMs: 80, sampleRate: 8000 },
  400: { name: "sotto 400", frameMs: 120, sampleRate: 8000 },
};

async function instantiate(source) {
  const bytes = source instanceof ArrayBuffer || ArrayBuffer.isView(source)
    ? source : await (await fetch(source)).arrayBuffer();
  const { instance } = await WebAssembly.instantiate(bytes, { wasi_snapshot_preview1: wasiStubs });
  const x = instance.exports;
  x._initialize();
  return x;
}

const cstr = (x, p) => {
  const m = new Uint8Array(x.memory.buffer);
  let e = p; while (m[e]) e++;
  return new TextDecoder().decode(m.subarray(p, e));
};

// Runs fn(pointers...) with n-byte blocks allocated in module x, freeing them afterwards.
function withBlocks(x, sizes, fn) {
  const ps = sizes.map(n => (n ? x.sotto_wasm_alloc(n) : 0));
  try { return fn(...ps); } finally { for (const p of ps) if (p) x.sotto_wasm_free(p); }
}

// frames (Uint8Array) and good (optional Uint8Array) into module memory, then decodeAt(pb, pg, n, po)
function runDecode(x, frames, good, outLen, decodeAt) {
  const n = Math.floor(frames.length / 6), nOut = outLen(n);
  return withBlocks(x, [6 * n, good ? n : 0, 2 * nOut], (pb, pg, po) => {
    new Uint8Array(x.memory.buffer, pb, 6 * n).set(frames.subarray(0, 6 * n));
    if (good) new Uint8Array(x.memory.buffer, pg, n).set(good.subarray(0, n));
    const w = decodeAt(pb, pg, n, po);
    if (w < 0) throw new Error("sotto: decoder allocation failed");
    return new Int16Array(x.memory.buffer, po, w).slice();
  });
}

function runEncode(x, speech, maxFrames, encodeAt) {
  const n = speech.length, max = maxFrames(n);
  return withBlocks(x, [2 * n, 6 * max], (ps, po) => {
    new Int16Array(x.memory.buffer, ps, n).set(speech);
    const nf = encodeAt(ps, n, po);
    if (nf < 0) throw new Error("sotto: encoder allocation failed");
    return new Uint8Array(x.memory.buffer, po, 6 * nf).slice();
  });
}

// source: sotto.wasm (URL or bytes); vlrSource: sotto_vlr.wasm, needed to decode 600 / 400.
export async function loadSotto(source, vlrSource) {
  const [x, v] = await Promise.all([instantiate(source), vlrSource ? instantiate(vlrSource) : null]);
  const down2 = pcm16 => withBlocks(x, [2 * pcm16.length, 2 * Math.ceil(pcm16.length / 2)], (pi, po) => {
    new Int16Array(x.memory.buffer, pi, pcm16.length).set(pcm16);
    const m = x.sotto_wasm_down2(pi, pcm16.length, po);
    return new Int16Array(x.memory.buffer, po, m).slice();
  });
  return {
    format: cstr(x, x.sotto_wasm_format()),
    rates: v ? [2400, 1200, 600, 400] : [2400, 1200],
    // 16 kHz to 8 kHz, as scipy's resample_poly(x, 1, 2) (the 600 / 400 encoders take 8 kHz)
    down2,
    // rate 2400, 1200, 600 or 400; speech: Int16Array at 16 kHz (see levelTo). Returns the
    // frames, the same bytes as `sotto_enc -16 [-1200]` (or `sotto_enc -600|-400` of down2's 8 kHz).
    encode(rate, speech) {
      if (rate === 2400) return this.encode16(speech);
      if (rate === 1200) return runEncode(x, speech, x.sotto_wasm_max_frames_lr16, x.sotto_wasm_encode_lr16);
      if (rate === 600 || rate === 400)
        return runEncode(x, down2(speech), n => x.sotto_wasm_max_frames_vlr(rate, n),
                         (ps, n, po) => x.sotto_wasm_encode_vlr(rate, ps, n, po));
      throw new Error(`sotto: no ${rate} bit/s mode`);
    },
    // frames at that rate; good: optional, one byte per frame, 0 = lost.
    // Returns { pcm: Int16Array, sampleRate } (16 kHz for 2400 and 1200, 8 kHz for 600 and 400).
    decode(rate, frames, good) {
      if (rate === 2400) return { pcm: this.decode16(frames, good), sampleRate: 16000 };
      if (rate === 1200)
        return { pcm: runDecode(x, frames, good, x.sotto_wasm_out_len_lr16, x.sotto_wasm_decode_lr16), sampleRate: 16000 };
      if ((rate === 600 || rate === 400) && v)
        return { pcm: runDecode(v, frames, good, n => v.sotto_wasm_vlr_out_len(rate, n),
                                (pb, pg, n, po) => v.sotto_wasm_decode_vlr(rate, pb, pg, n, po)), sampleRate: 8000 };
      throw new Error(`sotto: can't decode ${rate} bit/s here` + (v ? "" : " (sotto_vlr.wasm not loaded)"));
    },
    sampleRate: 16000,
    bytesPerFrame: 6,
    // speech: Int16Array at 16 kHz (speech near -26 dBFS uses the energy field best; see
    // levelTo). Returns Uint8Array of 6-byte frames, the same bytes as `sotto_enc -16`.
    encode16: speech => runEncode(x, speech, x.sotto_wasm_max_frames16, x.sotto_wasm_encode16),
    // frames: Uint8Array of 6-byte frames; good: optional Uint8Array, one byte per frame,
    // 0 where the frame failed its CRC. Returns Int16Array speech aligned with the encoder input.
    decode16: (frames, good) => runDecode(x, frames, good, x.sotto_wasm_out_len16, x.sotto_wasm_decode16),
  };
}

// Float samples (-1..1) to Int16Array with the active speech level at targetDb dBFS (RMS over
// the 10 ms blocks within 30 dB of the loudest, a rough ITU-T P.56), limited so peaks stay
// below full scale. Returns { pcm, gainDb }.
export function levelTo(x, targetDb = -26, rate = 16000) {
  const B = Math.round(rate / 100), e = [];
  for (let i = 0; i + B <= x.length; i += B) {
    let s = 0; for (let k = 0; k < B; k++) s += x[i + k] * x[i + k];
    e.push(s / B);
  }
  const top = Math.max(1e-12, ...e), act = e.filter(v => v > top * 1e-3);
  const rms = Math.sqrt(act.reduce((a, b) => a + b, 0) / Math.max(1, act.length));
  let peak = 1e-9; for (const v of x) peak = Math.max(peak, Math.abs(v));
  const g = Math.min(10 ** (targetDb / 20) / Math.max(rms, 1e-9), 0.97 / peak);
  const pcm = new Int16Array(x.length);
  for (let i = 0; i < x.length; i++) pcm[i] = Math.round(Math.max(-1, Math.min(1, x[i] * g)) * 32767);
  return { pcm, gainDb: 20 * Math.log10(g) };
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
