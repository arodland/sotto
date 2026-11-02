"""Build a labelled side-by-side page (original / A / B per row) from compare_page.html.

Usage: python tools/listening/build_compare.py SLUG "NAME" "A" "B" INTRO.html KEY.json OUT_DIR/index.html

KEY.json is {"trials": [{"n", "voice", "note", "kind"}, ...], "key": {n: {"wb_pesq": [a, b], ...}}};
GROUPS below names each kind's heading. The page expects audio/t{n}o.wav, t{n}a.wav (A) and
t{n}b.wav (B) next to it. See docs/listening-tests.md.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GROUPS = {"clean": "Clean voices", "lowpass": "Dull microphone (low-passed input)",
          "cut": "Band-limited recordings", "fade": "Over a fading FM channel"}


def main(slug, name, a, b, intro, key, out):
    s = open(os.path.join(HERE, "compare_page.html")).read()
    k = json.load(open(key))
    rows = []
    for t in k["trials"]:
        r = k["key"][str(t["n"])]
        sc = f"WB-PESQ {r['wb_pesq'][0]:.2f} → {r['wb_pesq'][1]:.2f}" if "wb_pesq" in r else ""
        rows.append(dict(n=t["n"], voice=t["voice"], note=t["note"], kind=t["kind"], score=sc))
    for x, y in (("{{SLUG}}", slug), ("{{NAME}}", name), ("{{A}}", a), ("{{B}}", b),
                 ("{{INTRO}}", open(intro).read().strip()), ("{{ROWS}}", json.dumps(rows, ensure_ascii=False, indent=1)),
                 ("{{GROUPS}}", json.dumps(GROUPS))):
        s = s.replace(x, y)
    open(out, "w").write(s)


if __name__ == "__main__":
    main(*sys.argv[1:8])
