"""M3spec §7 — fifteen required cases (+ §5 shape checks)."""

from __future__ import annotations

import unittest

from sql.pipeline import compile_sql
from tests.m3.helpers import setup_catalog, expect_error


class TestM3Cases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = setup_catalog()

    def test_01_self_join_renames(self):
        p = compile_sql(
            "SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid;",
            self.cat,
        )
        self.assertEqual(p["op"], "project")
        j = p["child"]
        self.assertEqual(j["op"], "join")
        self.assertEqual(j["left"]["op"], "rename")
        self.assertEqual(j["left"]["to"], "e")
        self.assertEqual(j["right"]["to"], "m")

    def test_02_star_three_tables_shared_name(self):
        p = compile_sql("SELECT * FROM Emp e, Dept d, T3;", self.cat)
        cols = p["columns"]
        self.assertIn("e.did", cols)
        self.assertIn("d.did", cols)
        self.assertIn("T3.did", cols)

    def test_03_ambiguous_column(self):
        e = expect_error(self.cat, "SELECT did FROM Emp, Dept;", "ambiguity")
        self.assertIn("Emp", e.message)
        self.assertIn("Dept", e.message)

    def test_04_union_vs_union_all(self):
        a = compile_sql(
            "SELECT city FROM Emp UNION ALL SELECT city FROM Dept;", self.cat
        )
        u = compile_sql(
            "SELECT city FROM Emp UNION SELECT city FROM Dept;", self.cat
        )
        self.assertEqual(a["op"], "union_all")
        self.assertEqual(u["op"], "union")

    def test_05_where_below_having_above(self):
        p = compile_sql(
            "SELECT did, COUNT(*) AS n FROM Emp WHERE age > 25 "
            "GROUP BY did HAVING COUNT(*) > 5;",
            self.cat,
        )
        # limit/sort absent: project > select(HAVING) > group_by > select(WHERE) > scan
        self.assertEqual(p["op"], "project")
        having = p["child"]
        self.assertEqual(having["op"], "select")
        self.assertIn("n >", having["condition"])
        gb = having["child"]
        self.assertEqual(gb["op"], "group_by")
        where = gb["child"]
        self.assertEqual(where["op"], "select")
        self.assertIn("age", where["condition"])

    def test_06_non_grouped_column(self):
        expect_error(
            self.cat,
            "SELECT name, did FROM Emp GROUP BY did;",
            "aggregate",
        )

    def test_07_aggregate_in_where(self):
        expect_error(
            self.cat,
            "SELECT name FROM Emp WHERE COUNT(*) > 1;",
            "aggregate",
        )

    def test_08_subquery_in_from(self):
        p = compile_sql(
            "SELECT t.did, t.n FROM "
            "(SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t "
            "WHERE t.n > 3;",
            self.cat,
        )
        self.assertEqual(p["columns"], ["t.did", "t.n"])
        ren = p["child"]["child"]
        self.assertEqual(ren["op"], "rename")
        self.assertEqual(ren["to"], "t")

    def test_09_order_by_not_in_select(self):
        p = compile_sql(
            "SELECT name FROM Emp ORDER BY age;", self.cat
        )
        self.assertEqual(p["op"], "sort")
        self.assertEqual(p["keys"][0][0], "Emp.age")
        self.assertEqual(p["child"]["columns"], ["Emp.name"])

    def test_10_type_error_int_vs_string(self):
        expect_error(
            self.cat,
            "SELECT name FROM Emp WHERE age > '30';",
            "type",
        )

    def test_11_unknown_table_vs_column(self):
        e1 = expect_error(self.cat, "SELECT name FROM NoSuch;", "name")
        self.assertIn("table", e1.message.lower())
        e2 = expect_error(self.cat, "SELECT no_col FROM Emp;", "name")
        self.assertIn("column", e2.message.lower())
        self.assertNotEqual(e1.message, e2.message)

    def test_12_duplicate_alias(self):
        expect_error(self.cat, "SELECT * FROM Emp, Emp;", "name")

    def test_13_count_star_no_groupby(self):
        p = compile_sql("SELECT COUNT(*) FROM Emp;", self.cat)
        self.assertEqual(p["op"], "project")
        gb = p["child"]
        self.assertEqual(gb["op"], "group_by")
        self.assertEqual(gb["keys"], [])
        self.assertEqual(gb["aggregates"][0][:2], ["count", "*"])

    def test_14_union_type_mismatch(self):
        expect_error(
            self.cat,
            "SELECT age FROM Emp UNION SELECT name FROM Emp;",
            "schema",
        )

    def test_15_comma_vs_join_on(self):
        comma = compile_sql(
            "SELECT * FROM Emp e, Dept d WHERE e.did = d.did;", self.cat
        )
        join = compile_sql(
            "SELECT * FROM Emp e JOIN Dept d ON e.did = d.did;", self.cat
        )
        # Walk to join/cross
        def find_op(n, ops):
            if n.get("op") in ops:
                return n
            for k in ("child", "left", "right"):
                if k in n:
                    r = find_op(n[k], ops)
                    if r:
                        return r
            return None

        self.assertIsNotNone(find_op(comma, {"cross"}))
        self.assertIsNotNone(find_op(join, {"join"}))
        self.assertIsNone(find_op(comma, {"join"}))


if __name__ == "__main__":
    unittest.main()
