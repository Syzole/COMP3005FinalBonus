"""Run Bonus 1 cases 1..25 (and optionally extras)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine.tokeniser import tokenise, LexError
from engine.parser import Parser, ParseError
from engine.eval import evaluate, format_result, EvalError
from engine.table import load_relation, Catalog


HERE = Path(__file__).resolve().parent


def normalize(text: str) -> str:
    """Ignore leading/trailing and per-line indent (fixtures vary on spacing)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return "\n".join(line.strip() for line in text.split("\n"))


def run_case(n: int) -> tuple[bool, str, str]:
    case = HERE / str(n)
    query = (case / "query.txt").read_text(encoding="utf-8")
    expected = normalize((case / "result.txt").read_text(encoding="utf-8"))
    data_path = case / "data.txt"
    data_text = data_path.read_text(encoding="utf-8") if data_path.is_file() else ""

    try:
        ast = Parser(tokenise(query)).parse()
        catalog = Catalog(load_relation(data_text)) if data_text.strip() else Catalog()
        got = normalize(format_result(evaluate(ast, catalog)))
    except LexError as e:
        got = normalize(f"error: lexical, {e}")
    except ParseError as e:
        got = normalize(f"error: syntax, {e}")
    except EvalError as e:
        got = normalize(f"error: {e}")
    except Exception as e:
        got = normalize(f"error: {e}")

    return got == expected, got, expected


def main():
    ap = argparse.ArgumentParser(description="Bonus 1 regression suite")
    ap.add_argument("--from", dest="start", type=int, default=1)
    ap.add_argument("--to", dest="end", type=int, default=25)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    passed = failed = 0
    for n in range(args.start, args.end + 1):
        case = HERE / str(n)
        if not (case / "query.txt").is_file() or not (case / "result.txt").is_file():
            print(f"[{n}] SKIP (missing files)")
            continue
        ok, got, expected = run_case(n)
        if ok:
            passed += 1
            print(f"[{n}] PASS")
        else:
            failed += 1
            print(f"[{n}] FAIL")
            if args.verbose:
                print("  --- got ---")
                print(got)
                print("  --- expected ---")
                print(expected)

    print(f"\n{passed} passed, {failed} failed (cases {args.start}..{args.end})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
