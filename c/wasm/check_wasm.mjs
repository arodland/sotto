// Decodes every docs/samples/v38/*.bit (with its .good file when there is one) with the
// WebAssembly decoder (docs/sotto_dec.wasm through docs/sotto-dec.js) and with the native
// c/sotto_dec -16, and compares the samples. Run from the repository root after `make` and
// `make -C c/wasm install`: node c/wasm/check_wasm.mjs [WASM]
// SPDX-License-Identifier: MIT
import { readFileSync, readdirSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadSotto } from "../../docs/sotto-dec.js";

const dir = "docs/samples/v38";
const sotto = await loadSotto(readFileSync(process.argv[2] || "docs/sotto_dec.wasm"));
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
rmSync(tmp, { recursive: true });
console.log(bad ? `FAIL: ${bad} clips differ (worst SNR ${worst.toFixed(1)} dB)` : "OK");
process.exit(bad ? 1 : 0);
