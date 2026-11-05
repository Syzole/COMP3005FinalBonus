"""exec run / exec trace."""

from __future__ import annotations

import argparse
import json
import sys

from exec.budget import ExecError
from exec.engine import execute_plan, trace_lines


def _load_plan(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ExecError(f"cannot read plan: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ExecError(
            f"JSON syntax error at line {e.lineno} column {e.colno}: {e.msg}"
        ) from e


def _run(args, trace: bool) -> int:
    try:
        plan = _load_plan(args.plan)
        result = execute_plan(args.db, plan, args.frames)
    except ExecError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if trace:
        print("\n".join(trace_lines(result["root"])))
        return 0
    if args.json:
        out = {
            "schema": result["schema"],
            "rows": result["rows"],
            "totals": result["totals"],
        }
        print(json.dumps(out))
    else:
        for row in result["rows"]:
            print(row)
        print(result["totals"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="exec")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--db", required=True)
    run_p.add_argument("--plan", required=True)
    run_p.add_argument("--frames", type=int, required=True)
    run_p.add_argument("--json", action="store_true")

    trace_p = sub.add_parser("trace")
    trace_p.add_argument("--db", required=True)
    trace_p.add_argument("--plan", required=True)
    trace_p.add_argument("--frames", type=int, required=True)

    args = parser.parse_args(argv)
    if args.frames < 1:
        print("frames must be >= 1", file=sys.stderr)
        return 1
    if args.cmd == "run":
        return _run(args, trace=False)
    return _run(args, trace=True)


if __name__ == "__main__":
    raise SystemExit(main())
