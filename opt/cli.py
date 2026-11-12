"""Module 4 CLI: analyze / rewrite / plan / explain / dp-table.

Entry: `python -m opt <subcommand> …`

Examples:
  python -m opt analyze  --db data/db_m3_test --table Emp --buckets 20
  python -m opt rewrite  --logical data/m4.json --before
  python -m opt plan     --db data/db_m3_test --logical data/m4.json --frames 32 --json
  python -m opt explain  --db data/db_m3_test --logical data/m4.json --frames 32
  python -m opt dp-table --db data/db_m3_test --logical data/m4.json --frames 32
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from storage.catalog import Catalog
from opt.analyze import analyze_table
from opt.physicalize import plan_physical, explain_physical, build_dp_for_joins
from opt.dp import dp_table_lines
from opt.rewrite import rewrite
from sql.logical_json import explain_tree


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="opt", description="Module 4 query optimizer")
    sub = p.add_subparsers(dest="cmd", required=True)

    # --- analyze -----------------------------------------------------------
    pa = sub.add_parser("analyze", help="scan a table and store stats")
    pa.add_argument("--db", "--catalog", dest="db", required=True)
    pa.add_argument("--table", required=True)
    pa.add_argument("--buckets", type=int, default=20)
    pa.add_argument("--frames", type=int, default=32)

    # --- rewrite (logical only; no --db) -----------------------------------
    pr = sub.add_parser("rewrite", help="apply logical rewrite rules 1–7")
    pr.add_argument("--logical", required=True, help="path to logical plan JSON")
    pr.add_argument("--json", action="store_true", help="print rewritten JSON")
    pr.add_argument(
        "--before",
        action="store_true",
        help="also print the input tree (for before/after compare)",
    )

    # --- plan / explain / dp-table -----------------------------------------
    def add_plan_args(sp):
        sp.add_argument("--db", "--catalog", dest="db", required=True)
        sp.add_argument("--logical", required=True, help="logical plan JSON file")
        sp.add_argument("--frames", type=int, default=32)

    pp = sub.add_parser("plan", help="rewrite + cost → physical JSON for M2")
    add_plan_args(pp)
    pp.add_argument("--json", action="store_true", help="pretty-print physical JSON")

    pe = sub.add_parser("explain", help="before/after tree with est_rows / est_cost")
    add_plan_args(pe)

    pd = sub.add_parser("dp-table", help="print Selinger left-deep DP table")
    add_plan_args(pd)

    args = p.parse_args(argv)
    try:
        if args.cmd == "analyze":
            cat = Catalog(args.db)
            stats = analyze_table(
                cat, args.table, buckets=args.buckets, frames=args.frames
            )
            print(json.dumps(stats, indent=2))
            return 0

        if args.cmd == "rewrite":
            text = Path(args.logical).read_text(encoding="utf-8")
            plan = json.loads(text)
            out = rewrite(plan)
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

        # plan / explain / dp-table all need catalog + logical JSON
        cat = Catalog(args.db)
        with open(args.logical, encoding="utf-8-sig") as f:
            logical = json.load(f)

        if args.cmd == "plan":
            phys = plan_physical(logical, cat, B=args.frames)
            if args.json:
                print(json.dumps(phys, indent=2))
            else:
                print(json.dumps(phys))
            return 0

        if args.cmd == "explain":
            before, phys, lines = explain_physical(logical, cat, B=args.frames)
            print("=== before rewrite ===")
            for line in explain_tree(before):
                print(line)
            print("=== after rewrite / physical ===")
            for line in lines:
                print(line)
            return 0

        if args.cmd == "dp-table":
            cell, table = build_dp_for_joins(logical, cat, B=args.frames)
            if not table:
                print("no multi-relation join to tabulate")
                return 0
            for line in dp_table_lines(table):
                print(line)
            if cell:
                print("--- best ---")
                print(json.dumps(cell.tree, indent=2))
            return 0

        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
