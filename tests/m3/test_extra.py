"""M3 confidence checks beyond the fifteen required cases.

Section 5 shapes, bag nodes, set-operator associativity, the four-relation
case 15, and the resolution and lexical rules the required list skips.
"""

from __future__ import annotations

import unittest

from sql.pipeline import compile_sql
from tests.m3.helpers import expect_error, setup_catalog

_PHYSICAL = {
    "seqscan",
    "filter",
    "hash_join",
    "nested_loop_join",
    "sort_merge_join",
    "index_scan",
}
_WORDS = {"and", "or", "not"}


def _walk(node, outputs: set, conditions: list) -> None:
    op = node.get("op")
    if op in _PHYSICAL:
        raise AssertionError(f"physical operator {op} in a logical plan")
    if op == "project":
        for col in node.get("columns", []):
            if "." not in col:
                outputs.add(col)
    if op == "group_by":
        for agg in node.get("aggregates", []):
            if len(agg) >= 3:
                outputs.add(agg[2])
    if op == "sort":
        for key, _direction in node.get("keys", []):
            if "." not in key:
                outputs.add(key)
    if isinstance(node.get("condition"), str):
        conditions.append(node["condition"])
    for child_key in ("child", "left", "right"):
        if child_key in node:
            _walk(node[child_key], outputs, conditions)


def _idents(condition: str) -> list[str]:
    out = []
    i = 0
    n = len(condition)
    while i < n:
        ch = condition[i]
        if ch == "'":
            i += 1
            while i < n:
                if condition[i] == "'" and i + 1 < n and condition[i + 1] == "'":
                    i += 2
                    continue
                if condition[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (condition[j].isalnum() or condition[j] in "._"):
                j += 1
            out.append(condition[i:j])
            i = j
            continue
        i += 1
    return out


def assert_logical(plan: dict) -> None:
    """M3spec §9: no physical ops, no bare column left in a condition."""
    outputs: set[str] = set()
    conditions: list[str] = []
    _walk(plan, outputs, conditions)
    for cond in conditions:
        for ident in _idents(cond):
            if ident.lower() in _WORDS:
                continue
            if "." in ident or ident in outputs:
                continue
            raise AssertionError(f"unqualified name {ident!r} in {cond!r}")


def _ops(node, name: str) -> list:
    found = []
    if node.get("op") == name:
        found.append(node)
    for key in ("child", "left", "right"):
        if key in node:
            found.extend(_ops(node[key], name))
    return found


def _has(node, name: str) -> bool:
    return bool(_ops(node, name))


class TestSection5Shapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = setup_catalog()

    def test_5_1_filter_and_project(self):
        plan = compile_sql("SELECT name, age FROM Emp WHERE age > 30;", self.cat)
        self.assertEqual(plan["op"], "project")
        self.assertEqual(plan["columns"], ["Emp.name", "Emp.age"])
        sel = plan["child"]
        self.assertEqual(sel["op"], "select")
        self.assertEqual(sel["condition"], "Emp.age > 30")
        self.assertEqual(sel["child"], {"op": "scan", "table": "Emp"})
        self.assertFalse(_has(plan, "distinct"))
        assert_logical(plan)

    def test_5_2_where_stays_above_the_join(self):
        plan = compile_sql(
            "SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did "
            "WHERE d.budget > 1000;",
            self.cat,
        )
        self.assertEqual(plan["op"], "project")
        sel = plan["child"]
        self.assertEqual(sel["op"], "select")
        self.assertEqual(sel["condition"], "d.budget > 1000")
        join = sel["child"]
        self.assertEqual(join["op"], "join")
        self.assertEqual(join["condition"], "e.did = d.did")
        self.assertEqual(join["left"]["to"], "e")
        self.assertEqual(join["right"]["to"], "d")
        assert_logical(plan)

    def test_5_4_having_order_limit(self):
        plan = compile_sql(
            "SELECT did, COUNT(*) AS n, AVG(salary) AS avg_sal FROM Emp "
            "WHERE age > 25 GROUP BY did HAVING COUNT(*) > 5 "
            "ORDER BY n DESC LIMIT 10;",
            self.cat,
        )
        self.assertEqual(plan["op"], "limit")
        self.assertEqual(plan["n"], 10)
        sort = plan["child"]
        self.assertEqual(sort["op"], "sort")
        self.assertEqual(sort["keys"], [["n", "desc"]])
        project = sort["child"]
        self.assertEqual(project["columns"], ["Emp.did", "n", "avg_sal"])
        having = project["child"]
        self.assertEqual(having["condition"], "n > 5")
        group = having["child"]
        self.assertEqual(group["op"], "group_by")
        self.assertEqual(group["keys"], ["Emp.did"])
        self.assertEqual(
            group["aggregates"],
            [["count", "*", "n"], ["avg", "Emp.salary", "avg_sal"]],
        )
        self.assertEqual(group["child"]["condition"], "Emp.age > 25")
        self.assertEqual(group["child"]["child"]["table"], "Emp")
        self.assertFalse(_has(plan, "distinct"))
        assert_logical(plan)

    def test_default_aggregate_names(self):
        plan = compile_sql("SELECT AVG(salary), COUNT(*) FROM Emp;", self.cat)
        self.assertEqual(plan["columns"], ["avg_Emp_salary", "count_star"])
        group = plan["child"]
        self.assertEqual(
            group["aggregates"],
            [["avg", "Emp.salary", "avg_Emp_salary"], ["count", "*", "count_star"]],
        )
        assert_logical(plan)


class TestBagsAndSets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = setup_catalog()

    def test_distinct_is_a_node_and_group_by_is_not(self):
        distinct = compile_sql("SELECT DISTINCT name FROM Emp;", self.cat)
        self.assertEqual(distinct["op"], "distinct")
        self.assertEqual(distinct["child"]["op"], "project")
        grouped = compile_sql(
            "SELECT DISTINCT did, COUNT(*) AS n FROM Emp GROUP BY did;", self.cat
        )
        self.assertFalse(_has(grouped, "distinct"))
        self.assertTrue(_has(grouped, "group_by"))
        assert_logical(distinct)
        assert_logical(grouped)

    def test_intersect_except_and_left_associativity(self):
        both = compile_sql(
            "SELECT city FROM Emp INTERSECT SELECT city FROM Dept;", self.cat
        )
        self.assertEqual(both["op"], "intersect")
        diff = compile_sql(
            "SELECT city FROM Emp EXCEPT SELECT city FROM Dept;", self.cat
        )
        self.assertEqual(diff["op"], "except")
        chain = compile_sql(
            "SELECT city FROM Emp UNION SELECT city FROM Dept "
            "EXCEPT SELECT city FROM Emp;",
            self.cat,
        )
        self.assertEqual(chain["op"], "except")
        self.assertEqual(chain["left"]["op"], "union")
        self.assertEqual(chain["right"]["op"], "project")
        assert_logical(chain)

    def test_union_arity_mismatch(self):
        expect_error(
            self.cat,
            "SELECT name, age FROM Emp UNION SELECT city FROM Emp;",
            "schema",
        )

    def test_four_relation_comma_versus_join(self):
        comma = compile_sql(
            "SELECT * FROM Emp e, Dept d, T3 t, Emp m "
            "WHERE e.did = d.did AND d.did = t.did AND e.mgr = m.eid;",
            self.cat,
        )
        joined = compile_sql(
            "SELECT * FROM Emp e "
            "JOIN Dept d ON e.did = d.did "
            "JOIN T3 t ON d.did = t.did "
            "JOIN Emp m ON e.mgr = m.eid;",
            self.cat,
        )
        self.assertEqual(len(_ops(comma, "cross")), 3)
        self.assertFalse(_has(comma, "join"))
        select = _ops(comma, "select")
        self.assertEqual(len(select), 1)
        text = select[0]["condition"]
        self.assertIn("e.did = d.did", text)
        self.assertIn("d.did = t.did", text)
        self.assertIn("e.mgr = m.eid", text)
        joins = _ops(joined, "join")
        self.assertEqual(len(joins), 3)
        self.assertFalse(_has(joined, "cross"))
        self.assertFalse(_has(joined, "select"))
        conds = {j["condition"] for j in joins}
        self.assertEqual(conds, {"e.did = d.did", "d.did = t.did", "e.mgr = m.eid"})
        assert_logical(comma)
        assert_logical(joined)


class TestNamesTypesLexing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = setup_catalog()

    def test_order_by_alias_beats_column(self):
        plan = compile_sql("SELECT salary AS age FROM Emp ORDER BY age;", self.cat)
        self.assertEqual(plan["keys"], [["age", "asc"]])
        self.assertEqual(plan["child"]["columns"], ["age"])
        assert_logical(plan)

    def test_qualified_errors_name_the_failing_part(self):
        missing_col = expect_error(self.cat, "SELECT e.nope FROM Emp e;", "name")
        missing_alias = expect_error(self.cat, "SELECT Ghost.eid FROM Emp;", "name")
        self.assertIn("column", missing_col.message.lower())
        self.assertIn("nope", missing_col.message)
        self.assertIn("alias", missing_alias.message.lower())
        self.assertIn("Ghost", missing_alias.message)
        self.assertNotEqual(missing_col.message, missing_alias.message)

    def test_subquery_requires_alias_and_hides_inner_columns(self):
        expect_error(
            self.cat,
            "SELECT * FROM (SELECT did FROM Emp);",
            "syntax",
        )
        missing = expect_error(
            self.cat,
            "SELECT t.salary FROM "
            "(SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t;",
            "name",
        )
        self.assertIn("column", missing.message.lower())
        self.assertIn("salary", missing.message)

    def test_sum_needs_int_min_accepts_string(self):
        expect_error(self.cat, "SELECT SUM(name) FROM Emp;", "type")
        expect_error(self.cat, "SELECT AVG(city) FROM Emp;", "type")
        plan = compile_sql("SELECT MIN(city) FROM Emp;", self.cat)
        self.assertEqual(plan["columns"], ["min_Emp_city"])
        assert_logical(plan)
        compile_sql("SELECT MAX(city) FROM Emp;", self.cat)

    def test_having_non_group_column(self):
        expect_error(
            self.cat,
            "SELECT did, COUNT(*) FROM Emp GROUP BY did HAVING name = 'a';",
            "aggregate",
        )

    def test_keywords_comments_strings_and_precedence(self):
        compile_sql("SeLeCt name fRoM Emp;", self.cat)
        expect_error(self.cat, "SELECT name FROM emp;", "name")
        commented = compile_sql(
            "SELECT name FROM Emp -- this would be a syntax error\nWHERE age > 30;",
            self.cat,
        )
        self.assertEqual(commented["child"]["condition"], "Emp.age > 30")
        quoted = compile_sql(
            "SELECT name FROM Emp WHERE city = 'o''brien';", self.cat
        )
        self.assertIn("o''brien", quoted["child"]["condition"])
        not_equal = compile_sql("SELECT name FROM Emp WHERE age <> 30;", self.cat)
        self.assertEqual(not_equal["child"]["condition"], "Emp.age != 30")
        prec = compile_sql(
            "SELECT name FROM Emp WHERE NOT age > 30 AND city = 'ottawa' OR did = 1;",
            self.cat,
        )
        self.assertEqual(
            prec["child"]["condition"],
            "((not (Emp.age > 30)) and (Emp.city = 'ottawa')) or (Emp.did = 1)",
        )
        assert_logical(prec)

    def test_error_format_has_line_and_column(self):
        try:
            compile_sql("SELECT name FROM Emp WHERE city = 'abc", self.cat)
            self.fail("expected a lexical error")
        except Exception as exc:
            text = exc.format()
        self.assertTrue(text.startswith("error: lexical:"))
        self.assertIn("at line ", text)
        self.assertIn("column ", text)
        typed = expect_error(
            self.cat,
            "SELECT name FROM Emp\nWHERE age > '30';",
            "type",
        )
        self.assertEqual(typed.line, 2)
        formatted = typed.format()
        self.assertTrue(formatted.startswith("error: type:"))
        self.assertIn("at line 2, column ", formatted)


if __name__ == "__main__":
    unittest.main()
