"""The page's WebAssembly codec (docs/sotto.wasm) against the native v3.8 encoder and decoder."""
import os, shutil, subprocess
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(not shutil.which("node") or not os.path.exists(os.path.join(ROOT, "c", "sotto_dec")),
                    reason="needs node and the C build (make)")
def test_wasm_decoder_matches_native():
    r = subprocess.run(["node", "c/wasm/check_wasm.mjs"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
