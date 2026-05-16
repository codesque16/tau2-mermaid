"""Compare two eval result JSON files side by side."""
from __future__ import annotations
import json, argparse


def main(baseline_path: str, finetuned_path: str):
    with open(baseline_path) as f:
        baseline = json.load(f)
    with open(finetuned_path) as f:
        ft = json.load(f)

    bsum = baseline["summary"]
    fsum = ft["summary"]

    all_keys = sorted(set(bsum.keys()) | set(fsum.keys()))
    print(f"{'metric':45} {'baseline':>12} {'finetuned':>12} {'delta':>10}")
    print("-" * 82)
    for k in all_keys:
        bv = bsum.get(k)
        fv = fsum.get(k)
        if bv is None or fv is None or not isinstance(bv, (int, float)):
            print(f"{k:45} {str(bv):>12} {str(fv):>12} {'-':>10}")
            continue
        delta = fv - bv
        marker = " ✓" if delta > 0 and "off_graph" not in k else (" ✗" if delta < 0 and "off_graph" not in k else "  ")
        # off_graph is the only metric where lower is better
        if "off_graph" in k:
            marker = " ✓" if delta < 0 else (" ✗" if delta > 0 else "  ")
        print(f"{k:45} {bv:>12.2f} {fv:>12.2f} {delta:>+10.2f}{marker}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--finetuned", required=True)
    args = ap.parse_args()
    main(args.baseline, args.finetuned)
