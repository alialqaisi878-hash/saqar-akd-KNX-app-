"""Builds the store-review / sales demo: the real app page with a fake house inside it (no tablet, no KNX).
usage: python3 demo/build.py app/src/main/assets/index.html out/demo.html"""
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
shim = open(os.path.join(here, "shim.js"), encoding="utf-8").read()
seed = open(os.path.join(here, "seed.js"), encoding="utf-8").read().strip()
app = open(sys.argv[1], encoding="utf-8").read().split("\n")
# insert right after the head's first lines, before the app's own scripts
at = next(i for i, l in enumerate(app) if l.startswith("<script"))
page = app[:at] + ["<script>", shim.replace("const SEED=__SEED__;", seed), "</script>"] + app[at:]
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(page))
print("demo page", len("\n".join(page)), "bytes")
