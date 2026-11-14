"""Shell tests: SQL text through Module 3, Module 4, Module 2, and Module 1."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from shell.cli import Session, main, run_sql


def _cli(db, command, frames=32, json_mode=False, script=None):
    argv = ["--db", str(db), "--frames", str(frames)]
    if json_mode:
        argv.append("--json")
    if script is not None:
        argv.extend(["-f", str(script)])
    else:
        argv.extend(["-c", command])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def _rows(db, query, frames=32):
    rc, out, err = _cli(db, query, frames=frames, json_mode=True)
    if rc != 0:
        raise AssertionError(err or out)
    payload = json.loads(out)
    return payload


class TestShell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, text: str) -> Path:
        path = self.db / "batch.sql"
        path.write_text(text, encoding="utf-8")
        return path

    def test_create_insert_keeps_duplicate_names(self):
        script = self._script(
            "CREATE TABLE Emp (name VARCHAR(20), age INT);\n"
            "INSERT INTO Emp VALUES ('sam', 20);\n"
            "INSERT INTO Emp VALUES ('sam', 30);\n"
        )
        rc, _out, err = _cli(self.db, None, script=script)
        self.assertEqual(rc, 0, err)
        payload = _rows(self.db, "SELECT name FROM Emp;")
        self.assertEqual(Counter(row[0] for row in payload["rows"]), Counter({"sam": 2}))

    def test_comma_join_matches_join_on(self):
        script = self._script(
            "CREATE TABLE Emp (name VARCHAR(20), did INT);\n"
            "CREATE TABLE Dept (did INT, title VARCHAR(20));\n"
            "INSERT INTO Emp VALUES ('alice', 1);\n"
            "INSERT INTO Emp VALUES ('bob', 1);\n"
            "INSERT INTO Emp VALUES ('carol', 2);\n"
            "INSERT INTO Dept VALUES (1, 'eng');\n"
            "INSERT INTO Dept VALUES (2, 'hr');\n"
        )
        rc, _out, err = _cli(self.db, None, script=script)
        self.assertEqual(rc, 0, err)
        comma = _rows(
            self.db,
            "SELECT e.name, d.title FROM Emp e, Dept d WHERE e.did = d.did;",
        )
        joined = _rows(
            self.db,
            "SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did;",
        )
        self.assertEqual(Counter(map(tuple, comma["rows"])), Counter(map(tuple, joined["rows"])))
        self.assertEqual(len(comma["rows"]), 3)

    def test_empty_count_and_sum(self):
        script = self._script("CREATE TABLE E (a INT);\n")
        rc, _out, err = _cli(self.db, None, script=script)
        self.assertEqual(rc, 0, err)
        payload = _rows(self.db, "SELECT COUNT(*) FROM E;")
        self.assertEqual(payload["rows"], [[0]])
        rc, _out, err = _cli(self.db, "SELECT SUM(a) FROM E;")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error:"))

    def test_union_drops_a_duplicate_union_all_keeps_it(self):
        script = self._script(
            "CREATE TABLE Emp (city VARCHAR(20));\n"
            "CREATE TABLE Dept (city VARCHAR(20));\n"
            "INSERT INTO Emp VALUES ('ottawa');\n"
            "INSERT INTO Emp VALUES ('ottawa');\n"
            "INSERT INTO Emp VALUES ('toronto');\n"
            "INSERT INTO Dept VALUES ('ottawa');\n"
            "INSERT INTO Dept VALUES ('toronto');\n"
        )
        rc, _out, err = _cli(self.db, None, script=script)
        self.assertEqual(rc, 0, err)
        union = _rows(
            self.db, "SELECT city FROM Emp UNION SELECT city FROM Dept;"
        )
        union_all = _rows(
            self.db, "SELECT city FROM Emp UNION ALL SELECT city FROM Dept;"
        )
        self.assertEqual(Counter(row[0] for row in union["rows"]), Counter({"ottawa": 1, "toronto": 1}))
        self.assertEqual(
            Counter(row[0] for row in union_all["rows"]),
            Counter({"ottawa": 3, "toronto": 2}),
        )

    def test_group_having_order_limit(self):
        script = self._script(
            "CREATE TABLE Emp (name VARCHAR(20), did INT);\n"
            "INSERT INTO Emp VALUES ('alice', 1);\n"
            "INSERT INTO Emp VALUES ('bob', 1);\n"
            "INSERT INTO Emp VALUES ('carol', 1);\n"
            "INSERT INTO Emp VALUES ('dave', 2);\n"
        )
        rc, _out, err = _cli(self.db, None, script=script)
        self.assertEqual(rc, 0, err)
        payload = _rows(
            self.db,
            "SELECT did, COUNT(*) AS n FROM Emp GROUP BY did "
            "HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 1;",
        )
        self.assertEqual(payload["rows"], [[1, 3]])

    def test_explain_and_explain_analyze(self):
        script = self._script(
            "CREATE TABLE Emp (name VARCHAR(20), age INT);\n"
            "INSERT INTO Emp VALUES ('alice', 35);\n"
        )
        self.assertEqual(_cli(self.db, None, script=script)[0], 0)
        rc, out, err = _cli(
            self.db, "EXPLAIN SELECT name FROM Emp WHERE age > 30;"
        )
        self.assertEqual(rc, 0, err)
        logical, _, physical = out.partition("=== physical ===")
        self.assertIn("logical", logical)
        self.assertNotIn("hash_join", logical)
        self.assertTrue(physical.strip())
        rc, out, err = _cli(
            self.db, "EXPLAIN ANALYZE SELECT name FROM Emp WHERE age > 30;"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("rows=", out)
        self.assertIn("reads=", out)

    def test_set_frames_keeps_the_same_rows(self):
        script = self._script(
            "CREATE TABLE Emp (name VARCHAR(20), age INT);\n"
            "INSERT INTO Emp VALUES ('alice', 35);\n"
            "INSERT INTO Emp VALUES ('bob', 25);\n"
        )
        self.assertEqual(_cli(self.db, None, script=script)[0], 0)
        wide = _rows(self.db, "SELECT name FROM Emp WHERE age > 30;", frames=32)
        session = Session(str(self.db), frames=32)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = run_sql(session, "SET frames = 4")
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("frames = 4", out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = run_sql(session, "SELECT name FROM Emp WHERE age > 30;", json_mode=True)
        self.assertEqual(rc, 0, err.getvalue())
        narrow = json.loads(out.getvalue())
        self.assertEqual(narrow["rows"], wide["rows"])

    def test_load_analyze_then_select(self):
        script = self._script("CREATE TABLE Emp (name VARCHAR(20), age INT);\n")
        self.assertEqual(_cli(self.db, None, script=script)[0], 0)
        csv = self.db / "emp.csv"
        csv.write_text("alice,35\nbob,25\n", encoding="utf-8")
        rc, _out, err = _cli(self.db, f"LOAD Emp FROM '{csv.as_posix()}'")
        self.assertEqual(rc, 0, err)
        rc, out, err = _cli(self.db, "ANALYZE Emp")
        self.assertEqual(rc, 0, err)
        self.assertIn("n_tuples", out)
        payload = _rows(self.db, "SELECT name FROM Emp;")
        self.assertEqual(Counter(row[0] for row in payload["rows"]), Counter({"alice": 1, "bob": 1}))

    def test_syntax_error_exits_one(self):
        script = self._script("CREATE TABLE Emp (name VARCHAR(20));\n")
        self.assertEqual(_cli(self.db, None, script=script)[0], 0)
        rc, _out, err = _cli(self.db, "SELECT FROM Emp;")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error:"))

    def test_same_query_twice_same_rows_and_reads(self):
        script = self._script(
            "CREATE TABLE Emp (name VARCHAR(20), age INT);\n"
            "INSERT INTO Emp VALUES ('alice', 35);\n"
            "INSERT INTO Emp VALUES ('bob', 25);\n"
        )
        self.assertEqual(_cli(self.db, None, script=script)[0], 0)
        query = "SELECT name FROM Emp WHERE age > 20;"
        first = _rows(self.db, query)
        second = _rows(self.db, query)
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(first["totals"]["page_reads"], second["totals"]["page_reads"])
        self.assertEqual(len(first["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
