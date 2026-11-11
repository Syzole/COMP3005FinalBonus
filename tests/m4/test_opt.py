"""M4 rewrite smoke tests (rules 1–7). ANALYZE / physicalize tests come later."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from sql.pipeline import compile_sql
from opt.rewrite import rewrite
from tests.m3.helpers import setup_catalog

ROOT = Path(__file__).resolve().parents[2]


class TestM4Rewrite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = setup_catalog()

    def test_rewrite_comma_to_join(self):
        """M4spec §3: comma join + AND filters → join on equality, filters pushed."""
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
        """Same shape via the checked-in logical plan (data/m4.json)."""
        logical = json.loads((ROOT / "data" / "m4.json").read_text(encoding="utf-8"))
        r = rewrite(logical)
        self.assertEqual(r["child"]["op"], "join")
        self.assertEqual(r["child"]["condition"], "e.did = d.did")


if __name__ == "__main__":
    unittest.main()
