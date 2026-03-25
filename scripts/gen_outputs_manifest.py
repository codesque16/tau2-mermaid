#!/usr/bin/env python3
"""Write outputs/manifest.json listing GEPA-style run folders under outputs/.

Run from repo root:
  uv run python scripts/gen_outputs_manifest.py

A directory is included if it contains at least one of:
  best_policy.md, candidate_tree.html, candidates.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "outputs"
    if not out_dir.is_dir():
        print(f"Missing outputs directory: {out_dir}", file=sys.stderr)
        manifest = {"runs": []}
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return 0

    markers = ("best_policy.md", "candidate_tree.html", "candidates.json")
    runs: list[str] = []
    for p in sorted(out_dir.iterdir(), key=lambda x: x.name.lower(), reverse=True):
        if not p.is_dir() or p.name.startswith("."):
            continue
        if any((p / m).is_file() for m in markers):
            runs.append(p.name)

    manifest = {"runs": runs}
    dest = out_dir / "manifest.json"
    dest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {dest} ({len(runs)} run(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
