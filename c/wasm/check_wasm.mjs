// Decodes every docs/samples/v38/*.bit (with its .good file when there is one) with the
// WebAssembly decoder (docs/sotto.wasm through docs/sotto.js) and with the native
// c/sotto_dec -16, and compares the samples; then encodes every *__original16.flac with both
// encoders (python3 with soundfile turns the FLAC into raw 16-bit; skipped without it) and
// compares the frames. Run from the repository root after `make` and
// `make -C c/wasm install`: node c/wasm/check_wasm.mjs [WASM]
// SPDX-License-Identifier: MIT
import { readFileSync, readdirSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadSotto } from "../../docs/sotto.js";

const dir = "docs/samples/v38";
const sotto = await loadSotto(readFileSync(process.argv[2] || "docs/sotto.wasm"));
console.log(`wasm decoder: ${sotto.format}`);
const tmp = mkdtempSync(join(tmpdir(), "sotto-wasm-"));
let worst = Infinity, bad = 0;
for (const f of readdirSync(dir).filter(f => f.endsWith(".bit")).sort()) {
  const bit = join(dir, f), goodPath = bit.replace(/\.bit$/, ".good");
  const good = existsSync(goodPath) ? new Uint8Array(readFileSync(goodPath)) : undefined;
  const w = sotto.decode16(new Uint8Array(readFileSync(bit)), good);
  const out = join(tmp, "out.raw");
  execFileSync("c/sotto_dec", ["-16", bit, out, ...(good ? [goodPath] : [])]);
  const raw = readFileSync(out);
  const c = new Int16Array(raw.buffer, raw.byteOffset, raw.length / 2);
  let same = 0, maxd = 0, se = 0, sd = 0;
  const n = Math.min(w.length, c.length);
  for (let i = 0; i < n; i++) {
    const d = w[i] - c[i];
    if (!d) same++;
    maxd = Math.max(maxd, Math.abs(d)); se += c[i] * c[i]; sd += d * d;
  }
  const snr = sd ? 10 * Math.log10(se / sd) : Infinity;
  worst = Math.min(worst, snr);
  if (w.length !== c.length || snr < 60) bad++;
  console.log(`${f}: ${w.length} vs ${c.length} samples, identical ${(100 * same / n).toFixed(2)}%, ` +
              `max diff ${maxd}, SNR ${snr === Infinity ? "inf" : snr.toFixed(1)} dB`);
}
// encoder: same 16-bit input to both encoders. The two libms differ in the last bit of a few
// results, which can flip a near-tie in the pitch tracker or the VQ search and change a run of
// frames, so the check asks for 97% identical frames (measured: 98-100%).
for (const f of readdirSync(dir).filter(f => f.endsWith("__original16.flac")).sort()) {
  const raw = join(tmp, "in.raw"), bit = join(tmp, "out.bit");
  try {
    execFileSync(process.env.PYTHON || "python3", ["-c",
      "import sys, numpy as np, soundfile as sf; x = sf.read(sys.argv[1], dtype='int16')[0]; x = x[:len(x) // 320 * 320]; x.astype('<i2').tofile(sys.argv[2])",
      join(dir, f), raw]);
  } catch { console.log(`${f}: skipped (needs python3 with numpy and soundfile)`); continue; }
  const r = readFileSync(raw);
  const w = sotto.encode16(new Int16Array(r.buffer, r.byteOffset, r.length / 2));
  execFileSync("c/sotto_enc", ["-16", raw, bit]);
  const c = new Uint8Array(readFileSync(bit));
  let same = 0;
  const nf = Math.min(w.length, c.length) / 6;
  for (let i = 0; i < nf; i++) {
    let eq = true; for (let k = 0; k < 6; k++) eq &&= w[6 * i + k] === c[6 * i + k];
    same += eq;
  }
  if (w.length !== c.length || same < 0.97 * nf) bad++;
  console.log(`${f}: encoder ${w.length / 6} vs ${c.length / 6} frames, ${same} identical`);
}
rmSync(tmp, { recursive: true });
console.log(bad ? `FAIL: ${bad} clips differ (worst decoder SNR ${worst.toFixed(1)} dB)` : "OK");
process.exit(bad ? 1 : 0);
