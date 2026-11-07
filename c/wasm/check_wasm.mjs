// Decodes every docs/samples/v38/*.bit (with its .good file when there is one) with the
// WebAssembly decoder (docs/sotto.wasm through docs/sotto.js) and with the native
// c/sotto_dec -16, and compares the samples; then encodes every *__original16.flac with both
// encoders (python3 with soundfile turns the FLAC into raw 16-bit; skipped without it) and
// compares the frames. Then the rate comparison (docs/samples/rates, tools/render_page_rates.py):
// each <voice>__<rate>.bit decoded by both (600 / 400: the native v3.7 fixed-point
// build/c37/fixed/sotto_vlr_dec_fx), and the original encoded at each rate by both.
// Run from the repository root after `make` and `make -C c/wasm install` (or `make -C c/wasm
// check`): node c/wasm/check_wasm.mjs [SOTTO_WASM [SOTTO_VLR_WASM]]
// SPDX-License-Identifier: MIT
import { readFileSync, readdirSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadSotto } from "../../docs/sotto.js";

const dir = "docs/samples/v38";
const sotto = await loadSotto(readFileSync(process.argv[2] || "docs/sotto.wasm"),
                              readFileSync(process.argv[3] || "docs/sotto_vlr.wasm"));
console.log(`wasm decoder: ${sotto.format}`);
const VLR_DEC = "build/c37/fixed/sotto_vlr_dec_fx";
const asI16 = b => new Int16Array(b.buffer, b.byteOffset, b.length / 2);
function compare(name, w, c, minSnr) {     // decoded samples, wasm against native
  let same = 0, maxd = 0, se = 0, sd = 0;
  const n = Math.min(w.length, c.length);
  for (let i = 0; i < n; i++) {
    const d = w[i] - c[i];
    if (!d) same++;
    maxd = Math.max(maxd, Math.abs(d)); se += c[i] * c[i]; sd += d * d;
  }
  const snr = sd ? 10 * Math.log10(se / sd) : Infinity;
  console.log(`${name}: ${w.length} vs ${c.length} samples, identical ${(100 * same / n).toFixed(2)}%, ` +
              `max diff ${maxd}, SNR ${snr === Infinity ? "inf" : snr.toFixed(1)} dB`);
  return { snr, ok: w.length === c.length && snr >= minSnr };
}
function sameFrames(w, c) {
  let same = 0;
  const nf = Math.min(w.length, c.length) / 6;
  for (let i = 0; i < nf; i++) {
    let eq = true; for (let k = 0; k < 6; k++) eq &&= w[6 * i + k] === c[6 * i + k];
    same += eq;
  }
  return { same, nf };
}
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
// encoder: same 16-bit input to both encoders. The two libms may differ in the last bit of a
// few results, which could flip a near-tie in the pitch tracker or the VQ search and change a run
// of frames, so the check asks for 97% identical frames. Measured: 100% on every clip, now that
// each call restarts Codec 2's random generator (before, frames after a decode differed: 98-100%).
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
// the four rates. The decoders are deterministic (the 600 / 400 one is integer only), so the
// samples must match closely; the encoders get the same 97% rule as above.
const rdir = "docs/samples/rates";
const voices = [...new Set(readdirSync(rdir).filter(f => f.endsWith(".bit")).map(f => f.split("__")[0]))].sort();
for (const v of voices) {
  const raw = join(tmp, "in.raw");
  try {
    execFileSync(process.env.PYTHON || "python3", ["-c",
      "import sys, numpy as np, soundfile as sf; x = sf.read(sys.argv[1], dtype='int16')[0]; x = x[:len(x) // 640 * 640]; x.astype('<i2').tofile(sys.argv[2])",
      join(dir, `${v}__original16.flac`), raw]);
  } catch { console.log(`${v}: rate encoders skipped (needs python3 with numpy and soundfile)`); }
  const x16 = existsSync(raw) ? asI16(readFileSync(raw)) : null;
  for (const rate of [2400, 1200, 600, 400]) {
    const bit = join(rdir, `${v}__${rate}.bit`), out = join(tmp, "out.raw");
    const frames = new Uint8Array(readFileSync(bit));
    const w = sotto.decode(rate, frames).pcm;
    if (rate >= 1200) execFileSync("c/sotto_dec", ["-16", ...(rate === 1200 ? ["-1200"] : []), bit, out]);
    else if (existsSync(VLR_DEC)) execFileSync(VLR_DEC, [String(rate), bit, out]);
    else { console.log(`${v}__${rate}.bit: decoder skipped (no ${VLR_DEC}; make -C c/wasm check builds it)`); }
    const r = !existsSync(out) ? { snr: Infinity, ok: true } : compare(`${v}__${rate}.bit`, w, asI16(readFileSync(out)), rate >= 1200 ? 60 : Infinity);
    worst = Math.min(worst, r.snr);
    if (!r.ok) bad++;
    if (existsSync(out)) rmSync(out);
    if (!x16) continue;
    const e = sotto.encode(rate, x16), { same, nf } = sameFrames(e, frames);
    if (e.length !== frames.length || same < 0.97 * nf) bad++;
    console.log(`${v} at ${rate}: encoder ${e.length / 6} vs ${frames.length / 6} frames, ${same} identical`);
  }
  if (x16) {     // the 600 / 400 encoders' 8 kHz input: sotto_wasm_down2 against scipy
    const d8 = sotto.down2(x16), p8 = join(tmp, "x8.raw");
    execFileSync(process.env.PYTHON || "python3", ["-c",
      "import sys, numpy as np; from scipy.signal import resample_poly; x = np.fromfile(sys.argv[1], '<i2'); np.clip(np.rint(resample_poly(x.astype(float), 1, 2)), -32768, 32767).astype('<i2').tofile(sys.argv[2])",
      raw, p8]);
    const r = compare(`${v} 16 to 8 kHz`, d8, asI16(readFileSync(p8)), 80);
    if (!r.ok) bad++;
    rmSync(raw);
  }
}
rmSync(tmp, { recursive: true });
console.log(bad ? `FAIL: ${bad} clips differ (worst decoder SNR ${worst.toFixed(1)} dB)` : "OK");
process.exit(bad ? 1 : 0);
