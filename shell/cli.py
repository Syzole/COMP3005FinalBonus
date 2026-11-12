"""Integrated database shell (general spec §6).

SQL text in → rows out through M3 → M4 → M2 → M1.
Also: EXPLAIN, EXPLAIN ANALYZE, ANALYZE, SET frames, LOAD.
Module CLIs (`storage` / `exec` / `sql` / `opt`) remain unchanged.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from storage.catalog import Catalog, CatalogError
from storage.loader import load_csv, LoadError
from sql.errors import SqlError
from sql.parser import parse
from sql import ast as A
from sql.pipeline import compile_sql
from sql.logical_json import explain_tree
from opt.analyze import analyze_table
from opt.physicalize import plan_physical, explain_physical
from opt.rewrite import rewrite
from exec.engine import execute_plan, trace_lines


BANNER = """COMP 3005 FinalBonus shell
  SET frames = N | LOAD table FROM 'file' | ANALYZE table
  EXPLAIN … | EXPLAIN ANALYZE … | SQL query;
  .quit / exit
"""


class Session:
    def __init__(self, db: str, frames: int = 32):
        self.db = str(db)
        self.frames = frames
        self.catalog = Catalog(self.db)

    def refresh(self) -> None:
        self.catalog = Catalog(self.db)


def _is_meta(sql: str) -> str | None:
    s = sql.strip().rstrip(";").strip()
    up = s.upper()
    if up.startswith("SET ") and "FRAMES" in up:
        return "set"
    if up.startswith("LOAD "):
        return "load"
    if up.startswith("ANALYZE ") and not up.startswith("EXPLAIN"):
        return "analyze"
    if up.startswith("EXPLAIN ANALYZE"):
        return "explain_analyze"
    if up.startswith("EXPLAIN"):
        return "explain"
    return None


def _parse_set_frames(sql: str) -> int:
    # SET frames = N
    parts = sql.strip().rstrip(";").replace("=", " ").split()
    if len(parts) < 3 or parts[0].upper() != "SET" or parts[1].upper() != "FRAMES":
        raise ValueError("usage: SET frames = N")
    return int(parts[2])


def _parse_load(sql: str) -> tuple[str, str]:
    # LOAD name FROM 'file' or LOAD name FROM file
    s = sql.strip().rstrip(";")
    tokens = shlex.split(s)
    if len(tokens) < 4 or tokens[0].upper() != "LOAD" or tokens[2].upper() != "FROM":
        raise ValueError("usage: LOAD table FROM 'file.csv'")
    return tokens[1], tokens[3]


def _parse_analyze(sql: str) -> tuple[str, int]:
    # ANALYZE table [BUCKETS n]
    parts = sql.strip().rstrip(";").split()
    if len(parts) < 2 or parts[0].upper() != "ANALYZE":
        raise ValueError("usage: ANALYZE table [BUCKETS n]")
    table = parts[1]
    buckets = 20
    if len(parts) >= 4 and parts[2].upper() == "BUCKETS":
        buckets = int(parts[3])
    return table, buckets


def _strip_explain(sql: str, analyze: bool) -> str:
    s = sql.strip()
    if analyze:
        # EXPLAIN ANALYZE <query>
        rest = s[len("EXPLAIN") :].strip()
        if rest.upper().startswith("ANALYZE"):
            rest = rest[len("ANALYZE") :].strip()
        return rest
    if s.upper().startswith("EXPLAIN"):
        return s[len("EXPLAIN") :].strip()
    return s


def run_sql(session: Session, sql: str, json_mode: bool = False) -> int:
    meta = _is_meta(sql)
    try:
        if meta == "set":
            session.frames = _parse_set_frames(sql)
            print(f"frames = {session.frames}")
            return 0

        if meta == "load":
            table, path = _parse_load(sql)
            n = load_csv(session.catalog, table, path, frames=session.frames)
            session.refresh()
            print(f"loaded {n} rows into {table}")
            return 0

        if meta == "analyze":
            table, buckets = _parse_analyze(sql)
            stats = analyze_table(
                session.catalog, table, buckets=buckets, frames=session.frames
            )
            session.refresh()
            print(
                json.dumps(
                    {
                        "table": table,
                        "n_tuples": stats["n_tuples"],
                        "n_pages": stats["n_pages"],
                        "buckets": stats["buckets"],
                    },
                    indent=2,
                )
            )
            return 0

        if meta == "explain":
            q = _strip_explain(sql, analyze=False)
            logical = compile_sql(q, session.catalog)
            before, phys, lines = explain_physical(
                logical, session.catalog, B=session.frames
            )
            print("=== logical (after rewrite path) ===")
            for line in explain_tree(rewrite(logical)):
                print(line)
            print("=== physical ===")
            for line in lines:
                print(line)
            return 0

        if meta == "explain_analyze":
            q = _strip_explain(sql, analyze=True)
            logical = compile_sql(q, session.catalog)
            phys = plan_physical(logical, session.catalog, B=session.frames)
            result = execute_plan(session.db, phys, session.frames)
            _b, _p, lines = explain_physical(logical, session.catalog, B=session.frames)
            print("=== plan (est) ===")
            for line in lines:
                print(line)
            print("=== actual ===")
            print(
                f"rows={len(result['rows'])}  "
                f"reads={result['totals']['page_reads']}  "
                f"writes={result['totals']['page_writes']}"
            )
            if result.get("root") is not None:
                for line in trace_lines(result["root"]):
                    print(line)
            return 0

        # Ordinary SQL: SELECT / CREATE / INSERT
        stmts = parse(sql)
        if not stmts:
            print("error: empty input", file=sys.stderr)
            return 1
        for stmt in stmts:
            if isinstance(stmt, A.CreateTable):
                # columns already [[name,"INT"]|[name,"VARCHAR",n]]
                session.catalog.create_table(stmt.name, list(stmt.columns))
                session.refresh()
                print(f"created table {stmt.name}")
                continue
            if isinstance(stmt, A.Insert):
                heap, pool, info = session.catalog.open_heap(
                    stmt.table, frames=session.frames
                )
                try:
                    row = [lit.value for lit in stmt.values]
                    heap.insert(row)
                    pool.flush_all()
                    live = heap.num_tuples()
                    session.catalog.update_table_stats(
                        stmt.table,
                        info["num_pages"],
                        live,
                        info.get("stats") or b"",
                    )
                finally:
                    pool.flush_all()
                session.refresh()
                print(f"inserted 1 row into {stmt.table}")
                continue
            if isinstance(stmt, (A.SelectBlock, A.SetOp)):
                logical = compile_sql(sql, session.catalog)
                phys = plan_physical(logical, session.catalog, B=session.frames)
                result = execute_plan(session.db, phys, session.frames)
                schema = result["schema"]
                if json_mode:
                    print(
                        json.dumps(
                            {
                                "schema": schema,
                                "rows": result["rows"],
                                "totals": result["totals"],
                            }
                        )
                    )
                else:
                    print(" | ".join(c[0] for c in schema))
                    print("-" * 40)
                    for row in result["rows"]:
                        print(" | ".join(str(v) for v in row))
                    print(
                        f"({len(result['rows'])} rows, "
                        f"reads={result['totals']['page_reads']}, "
                        f"writes={result['totals']['page_writes']})"
                    )
                continue
            print(f"error: unsupported statement {type(stmt).__name__}", file=sys.stderr)
            return 1
        return 0
    except (SqlError, CatalogError, LoadError, ValueError, Exception) as e:
        if isinstance(e, SqlError):
            print(str(e), file=sys.stderr)
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1


def repl(session: Session, json_mode: bool = False) -> int:
    print(BANNER)
    print(f"db={session.db}  frames={session.frames}")
    buf: list[str] = []
    while True:
        try:
            prompt = "sql> " if not buf else "...> "
            line = input(prompt)
        except EOFError:
            print()
            break
        if line.strip() in (".quit", "exit", "quit") and not buf:
            break
        buf.append(line)
        text = "\n".join(buf)
        if ";" not in text and not _is_meta(text):
            continue
        # allow meta without semicolon
        if _is_meta(text) and ";" not in text:
            run_sql(session, text, json_mode=json_mode)
            buf.clear()
            continue
        # split on semicolons for multi-statement
        parts = text.split(";")
        # last fragment may be incomplete
        complete = parts[:-1]
        rest = parts[-1]
        for part in complete:
            stmt = part.strip()
            if stmt:
                run_sql(session, stmt + ";", json_mode=json_mode)
        buf = [rest] if rest.strip() else []
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shell", description="Integrated SQL shell")
    p.add_argument("--db", required=True, help="database directory")
    p.add_argument("--frames", type=int, default=32)
    p.add_argument("--command", "-c", help="run one statement and exit")
    p.add_argument("--file", "-f", help="run statements from a file")
    p.add_argument(
        "--json",
        action="store_true",
        help="print each query result as JSON (schema, rows, totals)",
    )
    args = p.parse_args(argv)
    session = Session(args.db, frames=args.frames)
    if args.command:
        return run_sql(session, args.command, json_mode=args.json)
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8-sig")
        rc = 0
        for part in text.split(";"):
            stmt = part.strip()
            if stmt:
                rc = run_sql(session, stmt + ";", json_mode=args.json) or rc
        return rc
    return repl(session, json_mode=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
