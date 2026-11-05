"""Module 2 checks: sort I/O, joins, empty inputs, budget, cleanup.

Run from anywhere:
    python tests/m2/test_m2.py
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from exec.budget import ExecError
from exec.engine import execute_plan
from storage.catalog import Catalog
from storage.disk import PAGE_SIZE
from storage.loader import parse_schema
from storage.page import NO_SPACE, SlottedPage, encode_tuple


def tuples_per_page(schema, row) -> int:
    buf = bytearray(PAGE_SIZE)
    page = SlottedPage.fresh(buf, 0)
    rec = encode_tuple(schema, row)
    n = 0
    while page.insert(rec) != NO_SPACE:
        n += 1
    return n


def new_catalog(tmp: str) -> Catalog:
    return Catalog(tmp, frames=4, policy="lru")


def load_rows(cat: Catalog, name: str, schema_spec: str, rows: list) -> None:
    cat.create_table(name, parse_schema(schema_spec))
    heap, pool, _info = cat.open_heap(name, frames=4)
    for row in rows:
        heap.insert(row)
    pool.flush_all()
    if pool.pinned_count() != 0:
        raise AssertionError(f"{name} left pinned frames")


def load_pages(cat: Catalog, name: str, n_pages: int, width: int = 80):
    spec = f"a:INT,b:VARCHAR({width})"
    schema = parse_schema(spec)
    sample = [0, "x" * width]
    per = tuples_per_page(schema, sample)
    rows = [[i, "x" * width] for i in range(n_pages * per)]
    load_rows(cat, name, spec, rows)
    return per


def bag(rows):
    return Counter(tuple(r) for r in rows)


def expect_error(db, plan, frames, text: str) -> str:
    try:
        execute_plan(db, plan, frames)
    except ExecError as e:
        msg = str(e)
        if text not in msg:
            raise AssertionError(f"expected {text!r} in {msg!r}")
        return msg
    raise AssertionError(f"expected error containing {text!r}")


def no_temps(db: str) -> None:
    tmp = Path(db) / "_m2tmp"
    if tmp.exists():
        left = list(tmp.glob("*"))
        raise AssertionError(f"temp files remain: {left}")


def pass_count(n_pages: int, frames: int) -> int:
    if n_pages <= 0:
        return 0
    runs = math.ceil(n_pages / frames)
    if runs <= 1:
        return 1
    fan = frames - 1
    k = 0
    covered = 1
    while covered < runs:
        covered *= fan
        k += 1
    return 1 + k


def test_sort_example():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        per = load_pages(cat, "R", 8)
        plan = {
            "op": "sort",
            "keys": [["R.a", "asc"]],
            "child": {"op": "seqscan", "table": "R"},
        }
        result = execute_plan(tmp, plan, 3)
        root = result["root"]
        scan = root.children[0]
        reads = root.page_reads + scan.page_reads
        assert scan.page_reads == 8, scan.page_reads
        assert root.input_pages == 8, root.input_pages
        assert root.passes == 3, root.passes
        assert root.page_writes == 14, (root.page_writes, root.page_reads, reads)
        assert reads == 22, (reads, root.page_reads, root.page_writes)
        assert result["budget"].peak <= 3
        assert result["budget"].held == 0
        keys = [row[0] for row in result["rows"]]
        assert keys == list(range(8 * per))
        assert len(result["rows"]) == 8 * per
        no_temps(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sort_passes_and_large():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        frames = 4
        pages = 20 * frames
        per = load_pages(cat, "R", pages, width=40)
        plan = {
            "op": "sort",
            "keys": [["R.a", "asc"], ["R.b", "desc"]],
            "child": {"op": "seqscan", "table": "R"},
        }
        result = execute_plan(tmp, plan, frames)
        root = result["root"]
        assert root.input_pages == pages
        assert root.passes == pass_count(pages, frames), (root.passes, pass_count(pages, frames))
        assert result["budget"].peak <= frames
        keys = [row[0] for row in result["rows"]]
        assert keys == list(range(pages * per))
        no_temps(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sort_at_b4_bigger_than_memory():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        load_pages(cat, "R", 10, width=40)
        plan = {
            "op": "sort",
            "keys": [["R.a", "desc"]],
            "child": {"op": "seqscan", "table": "R"},
        }
        result = execute_plan(tmp, plan, 4)
        keys = [row[0] for row in result["rows"]]
        assert keys == sorted(keys, reverse=True)
        assert result["root"].input_pages == 10
        assert result["budget"].peak <= 4
        no_temps(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _join_db():
    tmp = tempfile.mkdtemp()
    cat = new_catalog(tmp)
    # Spec duplicate-key example, plus ids so the four pairings are visible.
    load_rows(cat, "R", "id:INT,b:INT", [[1, 1], [2, 2], [3, 2], [4, 3]])
    load_rows(cat, "S", "id:INT,b:INT", [[1, 2], [2, 2], [3, 4]])
    return tmp


def _join_plan(op: str):
    return {
        "op": op,
        "condition": "R.b = S.b",
        "left": {"op": "seqscan", "table": "R"},
        "right": {"op": "seqscan", "table": "S"},
    }


def test_duplicate_keys_and_three_joins():
    tmp = _join_db()
    try:
        bags = []
        nlj = None
        for op in ("nested_loop_join", "sort_merge_join", "hash_join"):
            result = execute_plan(tmp, _join_plan(op), 4)
            assert result["budget"].peak <= 4
            assert result["budget"].held == 0
            bags.append(bag(result["rows"]))
            if op == "nested_loop_join":
                nlj = result
            no_temps(tmp)
        assert bags[0] == bags[1] == bags[2]
        assert sum(bags[0].values()) == 4
        # pairings of the two R rows with b=2 and the two S rows with b=2
        assert bags[0][(2, 2, 1, 2)] == 1
        assert bags[0][(2, 2, 2, 2)] == 1
        assert bags[0][(3, 2, 1, 2)] == 1
        assert bags[0][(3, 2, 2, 2)] == 1
        assert nlj["root"].comparisons == 4 * 3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_smj_spills_only_when_the_run_does_not_fit():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        spec = "k:INT,v:VARCHAR(200)"
        schema = parse_schema(spec)
        per = tuples_per_page(schema, [0, "x" * 200])
        # Three pages of one key is more than the two pages the merge can hold at B = 4.
        n = 3 * per
        load_rows(cat, "R", spec, [[0, "x" * 200] for _ in range(n)])
        load_rows(cat, "S", spec, [[0, "x" * 200] for _ in range(n)])
        plan = _join_plan("sort_merge_join")
        plan["condition"] = "R.k = S.k"
        smj = execute_plan(tmp, plan, 4)
        plan["op"] = "nested_loop_join"
        nlj = execute_plan(tmp, plan, 4)
        assert smj["budget"].peak <= 4
        assert smj["budget"].held == 0
        assert len(smj["rows"]) == n * n
        assert bag(smj["rows"]) == bag(nlj["rows"])
        # Unique keys of the same width fit in the merge frames, so the join writes
        # only the two finished sorted runs.
        shutil.rmtree(tmp, ignore_errors=True)
        tmp = tempfile.mkdtemp()
        cat = new_catalog(tmp)
        load_rows(cat, "R", spec, [[i, "x" * 200] for i in range(n)])
        load_rows(cat, "S", spec, [[i, "x" * 200] for i in range(n)])
        plan = _join_plan("sort_merge_join")
        plan["condition"] = "R.k = S.k"
        unique = execute_plan(tmp, plan, 4)
        assert len(unique["rows"]) == n
        assert unique["totals"]["page_writes"] < n
        no_temps(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_join_larger_than_inputs_and_inequality():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        load_rows(cat, "R", "id:INT,b:INT", [[1, 1], [2, 1]])
        load_rows(cat, "S", "id:INT,b:INT", [[1, 1], [2, 1]])
        plan = _join_plan("hash_join")
        rows = execute_plan(tmp, plan, 4)["rows"]
        assert len(rows) == 4
        assert len(rows) > 2
        ineq = {
            "op": "nested_loop_join",
            "condition": "R.id < S.id",
            "left": {"op": "seqscan", "table": "R"},
            "right": {"op": "seqscan", "table": "S"},
        }
        got = bag(execute_plan(tmp, ineq, 4)["rows"])
        assert got[(1, 1, 2, 1)] == 1
        assert sum(got.values()) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hash_overflow_matches_nlj():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        # Three full pages of one key cannot fit in the B-1 = 2 build frames.
        spec = "k:INT,n:INT,pad:VARCHAR(100)"
        schema = parse_schema(spec)
        pad = "p" * 100
        per = tuples_per_page(schema, [1, 0, pad])
        pages = 3
        load_rows(cat, "R", spec, [[1, i, pad] for i in range(pages * per)])
        load_rows(cat, "S", spec, [[1, i, pad] for i in range(pages * per)])
        hj = {
            "op": "hash_join",
            "condition": "R.k = S.k",
            "left": {"op": "seqscan", "table": "R"},
            "right": {"op": "seqscan", "table": "S"},
        }
        nl = dict(hj)
        nl["op"] = "nested_loop_join"
        hrows = execute_plan(tmp, hj, 3)
        nrows = execute_plan(tmp, nl, 3)
        assert hrows["budget"].peak <= 3
        assert len(hrows["rows"]) == (pages * per) ** 2
        assert bag(hrows["rows"]) == bag(nrows["rows"])
        no_temps(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pipeline_and_limit():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        load_rows(cat, "R", "a:INT,b:VARCHAR(5)", [[1, "a"], [1, "a"], [2, "b"], [4, "c"], [0, "z"]])
        project = {
            "op": "project",
            "columns": ["R.a"],
            "child": {"op": "seqscan", "table": "R"},
        }
        rows = execute_plan(tmp, project, 3)["rows"]
        assert rows == [[1], [1], [2], [4], [0]]
        filt = {
            "op": "filter",
            "condition": "R.a > 0 and R.a > 1",
            "child": {"op": "seqscan", "table": "R"},
        }
        result = execute_plan(tmp, filt, 3)
        assert result["root"].comparisons == 5
        assert bag(result["rows"]) == bag([[2, "b"], [4, "c"]])
        load_pages(cat, "Big", 2, width=40)
        limit = {
            "op": "limit",
            "n": 1,
            "child": {"op": "seqscan", "table": "Big"},
        }
        result = execute_plan(tmp, limit, 3)
        assert result["rows"] == [[0, "x" * 40]]
        scan = result["root"].children[0]
        assert scan.page_reads == 1
        assert scan.tuples_out == 1
        zero = {"op": "limit", "n": 0, "child": {"op": "seqscan", "table": "Big"}}
        result = execute_plan(tmp, zero, 3)
        assert result["rows"] == []
        assert result["root"].children[0].page_reads == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_distinct_group_and_sets():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        load_rows(cat, "R", "dept:VARCHAR(5),salary:INT,name:VARCHAR(5)", [
            ["cs", 10, "ann"],
            ["cs", 30, "bob"],
            ["ee", 20, "cam"],
            ["cs", 10, "ann"],
        ])
        load_rows(cat, "S", "dept:VARCHAR(5),salary:INT,name:VARCHAR(5)", [
            ["cs", 10, "ann"],
            ["ee", 5, "dee"],
        ])
        for mode in ("sort", "hash"):
            distinct = {
                "op": "distinct",
                "mode": mode,
                "child": {"op": "project", "columns": ["R.dept", "R.name"],
                          "child": {"op": "seqscan", "table": "R"}},
            }
            got = bag(execute_plan(tmp, distinct, 4)["rows"])
            assert got == bag([["cs", "ann"], ["cs", "bob"], ["ee", "cam"]])
            group = {
                "op": "group_by",
                "mode": mode,
                "keys": ["R.dept"],
                "aggregates": [["count", "*"], ["avg", "R.salary"], ["max", "R.name"]],
                "child": {"op": "seqscan", "table": "R"},
            }
            result = execute_plan(tmp, group, 4)
            assert [c[0] for c in result["schema"]] == [
                "R.dept", "count_star", "avg_R_salary", "max_R_name",
            ]
            assert bag(result["rows"]) == bag([
                ["cs", 3, "16.6667", "bob"],
                ["ee", 1, "20.0000", "cam"],
            ])
        union_all = {
            "op": "union_all",
            "left": {"op": "project", "columns": ["R.dept"], "child": {"op": "seqscan", "table": "R"}},
            "right": {"op": "project", "columns": ["S.dept"], "child": {"op": "seqscan", "table": "S"}},
        }
        all_rows = execute_plan(tmp, union_all, 4)["rows"]
        assert bag(all_rows) == bag([["cs"], ["cs"], ["ee"], ["cs"], ["cs"], ["ee"]])
        for op, expected in (
            ("union", [["cs"], ["ee"]]),
            ("intersect", [["cs"], ["ee"]]),
            ("except", []),
        ):
            plan = {
                "op": op,
                "left": {"op": "project", "columns": ["R.dept"], "child": {"op": "seqscan", "table": "R"}},
                "right": {"op": "project", "columns": ["S.dept"], "child": {"op": "seqscan", "table": "S"}},
            }
            assert bag(execute_plan(tmp, plan, 4)["rows"]) == bag(expected)
        names = {
            "op": "except",
            "left": {"op": "project", "columns": ["R.name"], "child": {"op": "seqscan", "table": "R"}},
            "right": {"op": "project", "columns": ["S.name"], "child": {"op": "seqscan", "table": "S"}},
        }
        assert bag(execute_plan(tmp, names, 4)["rows"]) == bag([["bob"], ["cam"]])
        names["op"] = "intersect"
        assert bag(execute_plan(tmp, names, 4)["rows"]) == bag([["ann"]])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_inputs():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        load_rows(cat, "E", "a:INT,b:VARCHAR(5)", [])
        load_rows(cat, "F", "a:INT,b:VARCHAR(5)", [])
        scan = {"op": "seqscan", "table": "E"}
        plans = [
            scan,
            {"op": "filter", "condition": "E.a > 1", "child": scan},
            {"op": "project", "columns": ["E.a"], "child": scan},
            {"op": "sort", "keys": [["E.a", "asc"]], "child": scan},
            {"op": "distinct", "mode": "sort", "child": scan},
            {"op": "distinct", "mode": "hash", "child": scan},
            {"op": "limit", "n": 3, "child": scan},
            {"op": "group_by", "mode": "sort", "keys": ["E.a"], "aggregates": [["count", "*"]], "child": scan},
            {"op": "group_by", "mode": "hash", "keys": ["E.b"], "aggregates": [["min", "E.b"]], "child": scan},
            {"op": "union_all", "left": scan, "right": {"op": "seqscan", "table": "F"}},
            {"op": "union", "left": scan, "right": {"op": "seqscan", "table": "F"}},
            {"op": "intersect", "left": scan, "right": {"op": "seqscan", "table": "F"}},
            {"op": "except", "left": scan, "right": {"op": "seqscan", "table": "F"}},
            {
                "op": "nested_loop_join",
                "condition": "E.a = F.a",
                "left": scan,
                "right": {"op": "seqscan", "table": "F"},
            },
            {
                "op": "sort_merge_join",
                "condition": "E.a = F.a",
                "left": scan,
                "right": {"op": "seqscan", "table": "F"},
            },
            {
                "op": "hash_join",
                "condition": "E.a = F.a",
                "left": scan,
                "right": {"op": "seqscan", "table": "F"},
            },
        ]
        for plan in plans:
            result = execute_plan(tmp, plan, 4)
            assert result["rows"] == [], plan["op"]
            assert result["budget"].peak <= 4
            assert result["budget"].held == 0
            no_temps(tmp)
        count = {
            "op": "group_by",
            "mode": "sort",
            "keys": [],
            "aggregates": [["count", "*"]],
            "child": scan,
        }
        assert execute_plan(tmp, count, 4)["rows"] == [[0]]
        count["mode"] = "hash"
        assert execute_plan(tmp, count, 4)["rows"] == [[0]]
        bad = {
            "op": "group_by",
            "mode": "hash",
            "keys": [],
            "aggregates": [["sum", "E.a"]],
            "child": scan,
        }
        expect_error(tmp, bad, 4, "undefined")
        no_temps(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_errors_and_cli():
    tmp = tempfile.mkdtemp()
    try:
        cat = new_catalog(tmp)
        load_rows(cat, "R", "a:INT,b:VARCHAR(5)", [[1, "ab"]])
        expect_error(tmp, {"op": "sort", "child": {"op": "seqscan", "table": "R"}}, 4,
                     "Node 'sort': missing required field 'keys'.")
        expect_error(
            tmp,
            {"op": "filter", "condition": 1, "child": {"op": "seqscan", "table": "R"}},
            4,
            "Node 'filter': field 'condition' must be a string.",
        )
        expect_error(
            tmp,
            {"op": "merge_join", "condition": "R.a = R.a", "left": {"op": "seqscan", "table": "R"},
             "right": {"op": "seqscan", "table": "R"}},
            4,
            "Node 'merge_join': unknown operator 'merge_join'.",
        )
        expect_error(tmp, {"op": "seqscan", "table": "Z"}, 4, "unknown table 'Z'")
        expect_error(
            tmp,
            {"op": "filter", "condition": "R.z = 1", "child": {"op": "seqscan", "table": "R"}},
            4,
            "unknown column 'R.z'",
        )
        expect_error(
            tmp,
            {"op": "filter", "condition": "R.a = 'ab'", "child": {"op": "seqscan", "table": "R"}},
            4,
            "type error",
        )
        no_temps(tmp)
        plan_path = Path(tmp) / "plan.json"
        plan_path.write_text(json.dumps({"op": "seqscan", "table": "R"}), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "exec", "run", "--db", tmp, "--plan", str(plan_path),
             "--frames", "4", "--json"],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["rows"] == [[1, "ab"]]
        assert payload["totals"]["tuples_out"] == 1
        proc = subprocess.run(
            [sys.executable, "-m", "exec", "trace", "--db", tmp, "--plan", str(plan_path),
             "--frames", "4"],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "seqscan R" in proc.stdout
        bad = Path(tmp) / "bad.json"
        bad.write_text("{", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "exec", "run", "--db", tmp, "--plan", str(bad),
             "--frames", "4", "--json"],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 1
        assert "JSON syntax error" in proc.stderr
        assert "line" in proc.stderr
        no_temps(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tests = [
        test_sort_example,
        test_sort_passes_and_large,
        test_sort_at_b4_bigger_than_memory,
        test_duplicate_keys_and_three_joins,
        test_smj_spills_only_when_the_run_does_not_fit,
        test_join_larger_than_inputs_and_inequality,
        test_hash_overflow_matches_nlj,
        test_pipeline_and_limit,
        test_distinct_group_and_sets,
        test_empty_inputs,
        test_errors_and_cli,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
        else:
            print(f"PASS {test.__name__}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
