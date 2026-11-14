"""M4 confidence checks: published selectivity numbers, rule preconditions,
inequality joins, and the cardinality floor.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from exec.engine import execute_plan
from opt.analyze import analyze_table
from opt.cost import choose_join
from opt.physicalize import explain_physical, plan_physical
from opt.rewrite import rewrite
from opt.selectivity import join_cardinality, selectivity
from opt.stats import MARKER, stats_path
from sql.pipeline import compile_sql
from storage.catalog import Catalog
from storage.loader import load_csv
from tests.m4.test_opt import DB, find_join, setup


def _stats():
    """Hand-built stats for the M4spec §2 arithmetic."""
    return {
        "Emp": {
            "n_tuples": 10000,
            "n_pages": 100,
            "columns": {
                "did": {"ndv": 50, "min": 1, "max": 50, "hist": []},
                "age": {"ndv": 40, "min": 0, "max": 75, "hist": []},
            },
        },
        "Dept": {
            "n_tuples": 50,
            "n_pages": 1,
            "columns": {"did": {"ndv": 50, "min": 1, "max": 50, "hist": []}},
        },
    }


def _has(node, name: str) -> bool:
    if node.get("op") == name:
        return True
    for key in ("child", "left", "right"):
        if key in node and _has(node[key], name):
            return True
    return False


class TestSelectivity(unittest.TestCase):
    def test_published_formulas(self):
        stats = _stats()
        eq = selectivity("Emp.did = 7", stats, 10000)
        rng = selectivity("Emp.age > 60", stats, 10000)
        both = selectivity("Emp.did = 7 and Emp.age > 60", stats, 10000)
        either = selectivity("Emp.did = 7 or Emp.age > 60", stats, 10000)
        negated = selectivity("not (Emp.did = 7)", stats, 10000)
        # Plans store <> as != (engine lexer). That is the complement of equality.
        different = selectivity("Emp.did != 7", stats, 10000)
        self.assertAlmostEqual(eq, 0.02)
        self.assertAlmostEqual(rng, 0.20)
        self.assertAlmostEqual(both, 0.004)
        self.assertAlmostEqual(either, 0.216)
        self.assertAlmostEqual(negated, 0.98)
        self.assertAlmostEqual(different, 0.98)
        self.assertEqual(
            join_cardinality(10000, 50, "Emp.did = Dept.did", stats),
            10000,
        )
        self.assertEqual(
            join_cardinality(10000, 50, "Emp.age > Dept.budget", stats),
            50000,
        )


class TestRewritePreconditions(unittest.TestCase):
    def _join_tree(self, op: str, condition: str) -> dict:
        return {
            "op": "select",
            "condition": condition,
            "child": {
                "op": op,
                "condition": "e.did = d.did",
                "left": {
                    "op": "rename",
                    "to": "e",
                    "child": {"op": "scan", "table": "Emp"},
                },
                "right": {
                    "op": "rename",
                    "to": "d",
                    "child": {"op": "scan", "table": "Dept"},
                },
            },
        }

    def test_both_sides_stay_above_the_join(self):
        plan = self._join_tree("join", "e.age > d.budget")
        out = rewrite(plan)
        self.assertEqual(out["op"], "select")
        self.assertEqual(out["condition"], "e.age > d.budget")
        self.assertEqual(out["child"]["op"], "join")

    def test_inequality_does_not_become_a_join(self):
        plan = self._join_tree("cross", "e.age > d.budget")
        # cross nodes do not carry a condition; drop the leftover field.
        del plan["child"]["condition"]
        out = rewrite(plan)
        self.assertEqual(out["op"], "select")
        self.assertEqual(out["child"]["op"], "cross")
        self.assertFalse(_has(out, "join"))

    def test_group_column_moves_aggregate_predicate_stays(self):
        group = {
            "op": "group_by",
            "keys": ["Emp.did"],
            "aggregates": [["count", "*", "n"]],
            "child": {"op": "scan", "table": "Emp"},
        }
        pushed = rewrite(
            {"op": "select", "condition": "Emp.did = 1", "child": group}
        )
        self.assertEqual(pushed["op"], "group_by")
        self.assertEqual(pushed["child"]["op"], "select")
        self.assertEqual(pushed["child"]["condition"], "Emp.did = 1")
        stayed = rewrite({"op": "select", "condition": "n > 5", "child": group})
        self.assertEqual(stayed["op"], "select")
        self.assertEqual(stayed["condition"], "n > 5")
        self.assertEqual(stayed["child"]["op"], "group_by")

    def test_disabling_rule_3_leaves_the_cross(self):
        cat = setup()
        logical = compile_sql(
            "SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did;",
            cat,
        )
        out = rewrite(logical, disable={3})
        self.assertTrue(_has(out, "cross"))
        self.assertFalse(_has(out, "join"))


class TestPhysicalChoices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = setup()

    def test_alias_filter_still_returns_the_older_rows(self):
        logical = compile_sql(
            "SELECT e.name FROM Emp e WHERE e.age > 30;", self.cat
        )
        phys = plan_physical(logical, self.cat, B=8)
        result = execute_plan(str(DB), phys, 8)
        self.assertEqual(
            sorted(row[0] for row in result["rows"]),
            ["alice", "carol", "dave"],
        )

    def test_inequality_join_is_nested_loop(self):
        self.assertEqual(choose_join(10, 10, 32, equality=False)[0], "nested_loop_join")
        logical = compile_sql(
            "SELECT e.name FROM Emp e, Dept d WHERE e.age > d.budget;",
            self.cat,
        )
        phys = plan_physical(logical, self.cat, B=8)
        join = find_join(phys)
        self.assertIsNotNone(join)
        self.assertEqual(join["op"], "nested_loop_join")

    def test_estimate_never_drops_below_one(self):
        logical = compile_sql(
            "SELECT name FROM Emp WHERE age = -999999;", self.cat
        )
        _before, _phys, lines = explain_physical(logical, self.cat, B=8)
        estimates = []
        for line in lines:
            if "est_rows=" in line:
                estimates.append(int(line.split("est_rows=")[1].split()[0]))
        self.assertTrue(estimates)
        self.assertTrue(all(n >= 1 for n in estimates))

    def test_analyze_default_buckets_and_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)
            cat = Catalog(db)
            cat.create_table("Emp", [["age", "INT"], ["name", "VARCHAR", 10]])
            csv = db / "e.csv"
            csv.write_text("30,alice\n40,bob\n50,carol\n", encoding="utf-8")
            load_csv(cat, "Emp", csv, frames=8)
            stats = analyze_table(cat, "Emp")
            self.assertEqual(stats["buckets"], 20)
            for summary in stats["columns"].values():
                self.assertLessEqual(len(summary["hist"]), 20)
            self.assertEqual(cat.get_table("Emp")["stats"], MARKER)
            self.assertTrue(stats_path(db, "Emp").exists())


if __name__ == "__main__":
    unittest.main()
