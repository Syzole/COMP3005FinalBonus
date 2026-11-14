"""Round-trip every plan / condition node type through JSON."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine.tokeniser import tokenise
from engine.parser import Parser
from engine.JsonSerial import to_json, from_json
from engine.tree import (
    Relation, Project, Select, Rename, Distinct, Binary,
    Compare, Attr, Num, Str, Not, And, Or,
)
from engine.expr import parse_condition


def roundtrip(node):
    back = from_json(to_json(node))
    assert back == node, f"\n  orig={node!r}\n  back={back!r}"


class TestPlanJson(unittest.TestCase):
    def test_roundtrip_all_node_types(self):
        roundtrip(Attr(name="a"))
        roundtrip(Attr(name="b", relation="R"))
        roundtrip(Num(value=42))
        roundtrip(Str(value="'hi'"))
        roundtrip(Compare(op="=", left=Attr("a", "R"), right=Num(1)))
        roundtrip(And(
            left=Compare(op=">", left=Attr("a"), right=Num(0)),
            right=Compare(op="<", left=Attr("a"), right=Num(10)),
        ))
        roundtrip(Or(
            left=Compare(op="=", left=Attr("a"), right=Num(1)),
            right=Compare(op="=", left=Attr("a"), right=Num(2)),
        ))
        roundtrip(Not(cond=Compare(op="=", left=Attr("a"), right=Num(0))))

        roundtrip(Relation(name="Emp"))
        roundtrip(Project(attrs=["Name", "Emp.Age"], input=Relation("Emp")))
        roundtrip(Select(
            cond=Compare(op=">", left=Attr("Age"), right=Num(30)),
            input=Relation("Emp"),
        ))
        roundtrip(Rename(name="E2", input=Relation("Emp")))
        roundtrip(Distinct(input=Project(attrs=["DID"], input=Relation("Employees"))))
        roundtrip(Binary(op="union", left=Relation("A"), right=Relation("B")))
        roundtrip(Binary(op="intersect", left=Relation("A"), right=Relation("B")))
        roundtrip(Binary(op="minus", left=Relation("A"), right=Relation("B")))
        roundtrip(Binary(op="times", left=Relation("A"), right=Relation("B")))
        roundtrip(Binary(
            op="join",
            left=Relation("R"),
            right=Relation("S"),
            cond=Compare(op="=", left=Attr("b", "R"), right=Attr("b", "S")),
        ))

        for q in [
            "project[Name](select[Age>30](Employees))",
            "rename[E2](Emp) join[Emp.MgrID=E2.EID] Emp",
            "A union B minus C",
            "distinct(project[DID](Employees))",
            "project[Emp.Name](Emp)",
        ]:
            ast = Parser(tokenise(q)).parse()
            roundtrip(ast)

        c = parse_condition("R.b = S.b and R.a > 0")
        self.assertIsInstance(c, And)


if __name__ == "__main__":
    unittest.main()
