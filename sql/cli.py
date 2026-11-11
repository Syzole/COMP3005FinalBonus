"""Module 3 CLI: sql parse / compile / check."""

from __future__ import annotations

import argparse
import json
import sys

from sql.errors import SqlError
from sql.pipeline import compile_sql, compile_file, check_sql, ast_explain
from sql.logical_json import explain_tree
from storage.catalog import Catalog


def _db_arg(ns) -> str:
    # Accept either --db or legacy --catalog
    return getattr(ns, "db", None) or getattr(ns, "catalog", None)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sql", description="Module 3 SQL front end")
    sub = p.add_subparsers(dest="cmd", required=True)

    # parse: syntax only (no catalog / resolve)
    pp = sub.add_parser("parse", help="parse SQL (no catalog)")
    pp.add_argument("--query", required=True)
    pp.add_argument("--explain", action="store_true")

    # compile: full pipeline → logical plan (JSON or tree)
    pc = sub.add_parser("compile", help="compile to logical plan JSON")
    pc.add_argument("--db", "--catalog", dest="db", required=True)
    g = pc.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--query")
    pc.add_argument("--json", action="store_true")

    # check: compile for validation; print ok / SqlError
    ch = sub.add_parser("check", help="validate SQL against catalog")
    ch.add_argument("--db", "--catalog", dest="db", required=True)
    g2 = ch.add_mutually_exclusive_group(required=True)
    g2.add_argument("--file")
    g2.add_argument("--query")

    args = p.parse_args(argv)
    try:
        if args.cmd == "parse":
            if args.explain:
                # Indented AST outline (parse-only)
                for line in ast_explain(args.query):
                    print(line)
            else:
                from sql.parser import parse
                stmts = parse(args.query)
                print(f"ok: {len(stmts)} statement(s)")
            return 0

        cat = Catalog(args.db)  # load schema for resolve/translate
        if args.cmd == "compile":
            if args.file:
                plan = compile_file(args.file, cat)
            else:
                plan = compile_sql(args.query, cat)
            if args.json:
                print(json.dumps(plan, indent=2))
            else:
                # Default: human-readable operator tree
                for line in explain_tree(plan):
                    print(line)
            return 0

        # check — discard plan; just report success/failure
        src = open(args.file, encoding="utf-8-sig").read() if args.file else args.query
        check_sql(src, cat)
        print("ok")
        return 0
    except SqlError as e:
        print(e.format(), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
