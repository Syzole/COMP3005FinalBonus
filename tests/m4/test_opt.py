"""M4 checklist tests: rewrite, ANALYZE, physical plans in M2, cost/B."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from storage.catalog import Catalog
from storage.loader import load_csv
from sql.pipeline import compile_sql
from opt.analyze import analyze_table
from opt.rewrite import rewrite
from opt.physicalize import plan_physical
from opt.stats import unpack_stats
from opt.cost import choose_join
from opt.dp import RelInfo, optimize_joins
from exec.engine import execute_plan
from engine.expr import parse_condition
from engine.tree import And

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "db_m4_test"
DB_NOSTATS = ROOT / "data" / "db_m4_nostats"


def setup(db: Path = DB, analyze: bool = True) -> Catalog:
    """Tiny Emp/Dept DB; optionally run ANALYZE."""
    if db.exists():
        shutil.rmtree(db)
    db.mkdir(parents=True)
    cat = Catalog(db)
    cat.create_table(
        "Emp",
        [["name", "VARCHAR", 20], ["age", "INT"], ["did", "INT"], ["salary", "INT"]],
    )
    cat.create_table(
        "Dept", [["did", "INT"], ["title", "VARCHAR", 20], ["budget", "INT"]]
    )
    (ROOT / "data" / "_m4e.csv").write_text(
        "alice,35,1,50000\nbob,25,1,40000\ncarol,40,2,60000\ndave,55,1,70000\n",
        encoding="utf-8",
    )
    (ROOT / "data" / "_m4d.csv").write_text(
        "1,eng,2000\n2,hr,800\n", encoding="utf-8"
    )
    load_csv(cat, "Emp", ROOT / "data" / "_m4e.csv", frames=8)
    load_csv(cat, "Dept", ROOT / "data" / "_m4d.csv", frames=8)
    if analyze:
        analyze_table(cat, "Emp", buckets=4)
        analyze_table(cat, "Dept", buckets=4)
    return cat


def find_join(n):
    """First join node in a physical plan tree, or None."""
    if n.get("op") in ("nested_loop_join", "hash_join", "sort_merge_join"):
        return n
    for k in ("child", "left", "right"):
        if k in n:
            r = find_join(n[k])
            if r:
                return r
    return None


class TestM4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = setup(analyze=True)

    def test_analyze_idempotent(self):
        # Spec: ANALYZE twice → identical stats
        a = unpack_stats(self.cat.get_table("Emp")["stats"], DB, "Emp")
        analyze_table(self.cat, "Emp", buckets=4)
        b = unpack_stats(self.cat.get_table("Emp")["stats"], DB, "Emp")
        self.assertEqual(a, b)

    def test_rewrite_comma_to_join(self):
        # Spec §3: comma + AND → join on equality
        logical = compile_sql(
            "SELECT e.name FROM Emp e, Dept d "
            "WHERE e.did = d.did AND e.age > 30 AND d.budget > 1000;",
            self.cat,
        )
        r = rewrite(logical)
        self.assertEqual(r["op"], "project")
        self.assertEqual(r["child"]["op"], "join")
        self.assertEqual(r["child"]["condition"], "e.did = d.did")

    def test_rewrite_m4_json_fixture(self):
        logical = json.loads((ROOT / "data" / "m4.json").read_text(encoding="utf-8"))
        r = rewrite(logical)
        self.assertEqual(r["child"]["op"], "join")
        self.assertEqual(r["child"]["condition"], "e.did = d.did")

    def test_comma_and_join_same_physical_shape(self):
        # Comma vs JOIN ON → same physical join
        a = plan_physical(
            compile_sql(
                "SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did;",
                self.cat,
            ),
            self.cat,
            B=8,
        )
        b = plan_physical(
            compile_sql(
                "SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did;",
                self.cat,
            ),
            self.cat,
            B=8,
        )
        ja, jb = find_join(a), find_join(b)
        self.assertIsNotNone(ja)
        self.assertIsNotNone(jb)
        self.assertEqual(ja["condition"], jb["condition"])
        self.assertEqual(ja["op"], jb["op"])

    def test_physical_runs_in_m2(self):
        # Physical JSON must execute unedited
        logical = compile_sql(
            "SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did WHERE e.age > 30;",
            self.cat,
        )
        phys = plan_physical(logical, self.cat, B=8)
        result = execute_plan(str(DB), phys, 8)
        names = [r[0] for r in result["rows"]]
        self.assertIn("alice", names)

    def test_frames_can_change_join_algo(self):
        # Changing B / sizes flips preferred join
        self.assertEqual(choose_join(200, 200, B=4)[0], "hash_join")
        self.assertEqual(choose_join(2, 200, B=256)[0], "nested_loop_join")

    def test_no_stats_plan_runs(self):
        # Defaults path still yields a runnable plan
        cat = setup(db=DB_NOSTATS, analyze=False)
        logical = compile_sql(
            "SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did;",
            cat,
        )
        phys = plan_physical(logical, cat, B=8)
        self.assertIsNotNone(find_join(phys))
        result = execute_plan(str(DB_NOSTATS), phys, 8)
        self.assertGreater(len(result["rows"]), 0)

    def test_plan_uses_dp_join_order(self):
        # With tiny Dept and larger Emp, DP prefers Dept as outer for NLJ at large B
        logical = compile_sql(
            "SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did;",
            self.cat,
        )
        phys = plan_physical(logical, self.cat, B=256)
        j = find_join(phys)
        self.assertIsNotNone(j)
        # Walk to seqscans: cheaper outer should be Dept (2 rows) at large B
        def leftmost_scan(n):
            while "left" in n:
                n = n["left"]
            while "child" in n:
                n = n["child"]
            return n

        left = leftmost_scan(j)
        self.assertEqual(left.get("op"), "seqscan")
        self.assertEqual(left.get("table"), "Dept")

    def test_dp_section_5_1(self):
        # M4spec §5.1. Bp stands for B′. Width 81.92 = pages * 4096 / tuples,
        # so 1000 rows → 20 pages and 2000 rows → 40 pages.
        # The handout's losing plan writes 550 (one pass over A). ceil(40/30)
        # is 2, so that plan costs 650 here. 530 still wins; we do not force 550.
        width = 100 * 4096 / 5000

        def rel(rid, pages, tuples):
            return RelInfo(rid, rid, None, tuples, pages, width, None)

        relations = [rel("A", 100, 5000), rel("Bp", 10, 500), rel("C", 400, 20000)]
        edges = [("A", "Bp", "A.x = Bp.x"), ("Bp", "C", "Bp.y = C.y")]
        stats = {
            "A": {"n_tuples": 5000, "columns": {"x": {"ndv": 2500}}},
            "Bp": {
                "n_tuples": 500,
                "columns": {"x": {"ndv": 2500}, "y": {"ndv": 5000}},
            },
            "C": {"n_tuples": 20000, "columns": {"y": {"ndv": 5000}}},
        }
        _best, table = optimize_joins(relations, edges, 32, stats)
        self.assertNotIn(frozenset({"A", "C"}), table)

        ab = table[frozenset({"A", "Bp"})]
        bc = table[frozenset({"Bp", "C"})]
        full = table[frozenset({"A", "Bp", "C"})]
        self.assertEqual(ab.cost, 110)
        self.assertEqual(ab.pages, 20)
        self.assertEqual(bc.cost, 410)
        self.assertEqual(bc.pages, 40)
        self.assertEqual(full.cost, 530)
        self.assertEqual(full.tree["op"], "nested_loop_join")

    def test_dp_cartesian_when_disconnected(self):
        relations = [
            RelInfo("R", "R", None, 10, 1, 32.0, None),
            RelInfo("S", "S", None, 10, 1, 32.0, None),
        ]
        cell, table = optimize_joins(relations, [], 32, {})
        self.assertIn(frozenset({"R", "S"}), table)
        self.assertEqual(cell.tree["condition"], "1 = 1")
        self.assertEqual(cell.tree["op"], "nested_loop_join")

    def test_dp_rejects_more_than_six(self):
        relations = [
            RelInfo(f"R{i}", f"R{i}", None, 10, 1, 32.0, None) for i in range(7)
        ]
        with self.assertRaises(ValueError):
            optimize_joins(relations, [], 32, {})

    def test_rule7_collapses_adjacent_selects(self):
        plan = {
            "op": "select",
            "condition": "e.age > 30",
            "child": {
                "op": "select",
                "condition": "e.age < 60",
                "child": {"op": "scan", "table": "Emp"},
            },
        }
        out = rewrite(plan)
        self.assertEqual(out["op"], "select")
        self.assertEqual(out["child"]["op"], "scan")
        self.assertEqual(out["child"]["table"], "Emp")
        self.assertIsInstance(parse_condition(out["condition"]), And)

    def test_rule7_drops_reordered_project(self):
        plan = {
            "op": "project",
            "columns": ["e.name", "e.age"],
            "child": {
                "op": "project",
                "columns": ["e.age", "e.name"],
                "child": {"op": "scan", "table": "Emp"},
            },
        }
        out = rewrite(plan)
        self.assertEqual(out["op"], "project")
        self.assertEqual(sorted(out["columns"]), ["e.age", "e.name"])
        self.assertEqual(out["child"]["op"], "scan")

    def test_shell_json_query(self):
        import io
        from contextlib import redirect_stdout

        from shell.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--db", str(DB), "--frames", "8", "--json", "-c", "SELECT name FROM Emp;"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertIn("schema", payload)
        self.assertIn("rows", payload)
        self.assertIn("page_reads", payload["totals"])
        self.assertIn("page_writes", payload["totals"])
        self.assertIn("alice", [row[0] for row in payload["rows"]])


if __name__ == "__main__":
    unittest.main()
