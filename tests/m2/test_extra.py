"""M2 confidence checks the worked-example script does not lock.

Set-operator schema, identical counters, cleanup after a spilling plan,
and intersect when both inputs are larger than B.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
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


def _tuples_per_page(schema, row) -> int:
    page = SlottedPage.fresh(bytearray(PAGE_SIZE), 0)
    rec = encode_tuple(schema, row)
    n = 0
    while page.insert(rec) != NO_SPACE:
        n += 1
    return n


def _load(cat: Catalog, name: str, spec: str, rows: list) -> None:
    cat.create_table(name, parse_schema(spec))
    heap, pool, _info = cat.open_heap(name, frames=4)
    for row in rows:
        heap.insert(row)
    pool.flush_all()


def _bag(rows):
    return Counter(tuple(r) for r in rows)


def _no_temps(db: str) -> None:
    tmp = Path(db) / "_m2tmp"
    if tmp.exists():
        left = list(tmp.glob("*"))
        if left:
            raise AssertionError(f"temp files remain: {left}")


class TestSetSchema(unittest.TestCase):
    def test_arity_and_type_rejected_before_rows(self):
        tmp = tempfile.mkdtemp()
        try:
            cat = Catalog(tmp, frames=4)
            _load(cat, "R", "a:INT,b:VARCHAR(5)", [[1, "ab"]])
            _load(cat, "S", "x:INT,y:VARCHAR(5)", [[1, "ab"]])
            scan_r = {"op": "seqscan", "table": "R"}
            scan_s = {"op": "seqscan", "table": "S"}
            one = {"op": "project", "columns": ["R.a"], "child": scan_r}
            two = {"op": "project", "columns": ["S.x", "S.y"], "child": scan_s}
            text = {"op": "project", "columns": ["S.y"], "child": scan_s}
            for plan in (
                {"op": "union", "left": one, "right": two},
                {"op": "union", "left": one, "right": text},
                {"op": "intersect", "left": one, "right": text},
            ):
                with self.assertRaises(ExecError) as cm:
                    execute_plan(tmp, plan, 4)
                self.assertIn("union-compatible", str(cm.exception))
            ok = {"op": "union", "left": one, "right": {"op": "project", "columns": ["S.x"], "child": scan_s}}
            result = execute_plan(tmp, ok, 4)
            self.assertEqual(result["schema"][0][0], "R.a")
            self.assertEqual(result["rows"], [[1]])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_output_names_come_from_the_left(self):
        tmp = tempfile.mkdtemp()
        try:
            cat = Catalog(tmp, frames=4)
            _load(cat, "R", "a:INT", [[1], [2]])
            _load(cat, "S", "x:INT", [[2], [3]])
            plan = {
                "op": "intersect",
                "left": {"op": "project", "columns": ["R.a"], "child": {"op": "seqscan", "table": "R"}},
                "right": {"op": "project", "columns": ["S.x"], "child": {"op": "seqscan", "table": "S"}},
            }
            result = execute_plan(tmp, plan, 4)
            self.assertEqual(result["schema"][0][0], "R.a")
            self.assertEqual(result["rows"], [[2]])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestDeterminismAndCleanup(unittest.TestCase):
    def test_same_plan_same_counters(self):
        tmp = tempfile.mkdtemp()
        try:
            cat = Catalog(tmp, frames=4)
            _load(cat, "R", "a:INT,b:INT", [[1, 10], [2, 20], [2, 30]])
            _load(cat, "S", "a:INT,c:INT", [[2, 7], [2, 8], [3, 9]])
            plan = {
                "op": "nested_loop_join",
                "condition": "R.a = S.a",
                "left": {"op": "seqscan", "table": "R"},
                "right": {"op": "seqscan", "table": "S"},
            }
            first = execute_plan(tmp, plan, 4)
            second = execute_plan(tmp, plan, 4)
            self.assertEqual(first["rows"], second["rows"])
            self.assertEqual(first["totals"], second["totals"])
            self.assertEqual(len(first["rows"]), 4)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_spilling_plan_releases_frames_and_temps(self):
        tmp = tempfile.mkdtemp()
        try:
            cat = Catalog(tmp, frames=4)
            spec = "a:INT,b:VARCHAR(200)"
            schema = parse_schema(spec)
            per = _tuples_per_page(schema, [0, "x" * 200])
            rows = [[i, "x" * 200] for i in range(6 * per)]
            _load(cat, "R", spec, rows)
            plan = {
                "op": "sort",
                "keys": [["R.a", "asc"]],
                "child": {"op": "seqscan", "table": "R"},
            }
            result = execute_plan(tmp, plan, 4)
            self.assertEqual(result["budget"].held, 0)
            self.assertGreater(result["totals"]["page_writes"], 0)
            _no_temps(tmp)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestIntersectBiggerThanMemory(unittest.TestCase):
    def test_intersect_over_b_returns_each_overlap_once(self):
        tmp = tempfile.mkdtemp()
        try:
            cat = Catalog(tmp, frames=4)
            spec = "a:INT,b:VARCHAR(200)"
            schema = parse_schema(spec)
            per = _tuples_per_page(schema, [0, "x" * 200])
            n = 5 * per
            _load(cat, "R", spec, [[i, "x" * 200] for i in range(n)])
            # Overlap is the even keys. Each appears once on each side.
            _load(cat, "S", spec, [[i, "x" * 200] for i in range(0, n, 2)])
            plan = {
                "op": "intersect",
                "left": {"op": "project", "columns": ["R.a"], "child": {"op": "seqscan", "table": "R"}},
                "right": {"op": "project", "columns": ["S.a"], "child": {"op": "seqscan", "table": "S"}},
            }
            result = execute_plan(tmp, plan, 4)
            self.assertEqual(_bag(result["rows"]), _bag([[i] for i in range(0, n, 2)]))
            self.assertLessEqual(result["budget"].peak, 4)
            self.assertEqual(result["budget"].held, 0)
            _no_temps(tmp)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
