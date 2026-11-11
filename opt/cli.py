"""Module 4 CLI.

Entry: `python -m opt <subcommand> …`

Currently:
  rewrite   apply logical rewrite rules 1–7 to a plan JSON file

Coming later (M4spec §6):
  analyze   scan a table and store stats in the catalog
  plan      rewrite + cost → physical JSON for the M2 executor
  explain   indented tree with est_rows / est_cost per node
  dp-table  print the Selinger DP table for join enumeration

Example (Emp/Dept comma-join fixture):
  python -m opt rewrite --logical data/m4.json --before
  python -m opt rewrite --logical data/m4.json --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sql.logical_json import explain_tree
from opt.rewrite import rewrite


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="opt", description="Module 4 query optimizer")
    sub = p.add_subparsers(dest="cmd", required=True)

    # --- rewrite -----------------------------------------------------------
    # Pure logical transform: no --db / stats needed. Good first smoke test
    # against data/m4.json (the §3 worked example "before" tree).
    pr = sub.add_parser("rewrite", help="apply logical rewrite rules 1–7")
    pr.add_argument("--logical", required=True, help="path to logical plan JSON")
    pr.add_argument("--json", action="store_true", help="print rewritten JSON")
    pr.add_argument(
        "--before",
        action="store_true",
        help="also print the input tree (for before/after compare)",
    )

    args = p.parse_args(argv)
    try:
        if args.cmd == "rewrite":
            text = Path(args.logical).read_text(encoding="utf-8")
            plan = json.loads(text)
            out = rewrite(plan)

            # Default output is the indented operator tree (same style as
            # `sql compile`). --json dumps the plan dict for piping into later tools.
            if args.before:
                print("=== before ===")
                for line in explain_tree(plan):
                    print(line)
                print("=== after ===")
            if args.json:
                print(json.dumps(out, indent=2))
            else:
                for line in explain_tree(out):
                    print(line)
            return 0

        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
