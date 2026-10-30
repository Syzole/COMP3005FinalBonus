"""Serialize / deserialize plan trees as JSON (logical ops aligned with M3)."""

from __future__ import annotations

import json
from engine.tree import (
    Relation, Project, Select, Rename, Distinct, Binary,
    Compare, Attr, Num, Str, Not, And, Or,
    format_condition, format_operand,
)
from engine.expr import parse_condition


# RA Binary.op -> logical JSON op
_BIN_TO_JSON = {
    "union": "union",
    "intersect": "intersect",
    "minus": "except",
    "times": "cross",
    "join": "join",
}
_JSON_TO_BIN = {
    "union": "union",
    "intersect": "intersect",
    "except": "minus",
    "minus": "minus",
    "cross": "times",
    "times": "times",
    "join": "join",
}


def _cond_to_json(node) -> dict:
    if isinstance(node, Compare):
        return {
            "op": "compare",
            "cmp": node.op,
            "left": _operand_to_json(node.left),
            "right": _operand_to_json(node.right),
        }
    if isinstance(node, And):
        return {"op": "and", "left": _cond_to_json(node.left), "right": _cond_to_json(node.right)}
    if isinstance(node, Or):
        return {"op": "or", "left": _cond_to_json(node.left), "right": _cond_to_json(node.right)}
    if isinstance(node, Not):
        return {"op": "not", "cond": _cond_to_json(node.cond)}
    raise TypeError(f"not a condition node: {type(node)}")


def _operand_to_json(node) -> dict:
    if isinstance(node, Attr):
        d = {"op": "attr", "name": node.name}
        if node.relation is not None:
            d["relation"] = node.relation
        return d
    if isinstance(node, Num):
        return {"op": "num", "value": node.value}
    if isinstance(node, Str):
        return {"op": "str", "value": node.value}
    raise TypeError(f"not an operand: {type(node)}")


def _cond_from_json(d):
    if isinstance(d, str):
        return parse_condition(d)
    op = d["op"]
    if op == "compare":
        return Compare(op=d["cmp"], left=_operand_from_json(d["left"]), right=_operand_from_json(d["right"]))
    if op == "and":
        return And(left=_cond_from_json(d["left"]), right=_cond_from_json(d["right"]))
    if op == "or":
        return Or(left=_cond_from_json(d["left"]), right=_cond_from_json(d["right"]))
    if op == "not":
        return Not(cond=_cond_from_json(d["cond"]))
    raise ValueError(f"unknown condition op: {op}")


def _operand_from_json(d):
    op = d["op"]
    if op == "attr":
        return Attr(name=d["name"], relation=d.get("relation"))
    if op == "num":
        return Num(value=d["value"])
    if op == "str":
        return Str(value=d["value"])
    raise ValueError(f"unknown operand op: {op}")


def to_json(node) -> dict:
    """Convert an AST node to a JSON-serializable dict (logical plan)."""
    if isinstance(node, Relation):
        return {"op": "scan", "table": node.name}
    if isinstance(node, Select):
        return {
            "op": "select",
            "condition": _cond_to_json(node.cond),
            "condition_text": format_condition(node.cond),
            "child": to_json(node.input),
        }
    if isinstance(node, Project):
        return {"op": "project", "columns": list(node.attrs), "child": to_json(node.input)}
    if isinstance(node, Rename):
        return {"op": "rename", "to": node.name, "child": to_json(node.input)}
    if isinstance(node, Distinct):
        return {"op": "distinct", "child": to_json(node.input)}
    if isinstance(node, Binary):
        jop = _BIN_TO_JSON.get(node.op)
        if jop is None:
            raise ValueError(f"unknown binary op: {node.op}")
        out = {"op": jop, "left": to_json(node.left), "right": to_json(node.right)}
        if node.cond is not None:
            out["condition"] = _cond_to_json(node.cond)
            out["condition_text"] = format_condition(node.cond)
        return out
    # Bare condition / operand trees (for round-trip tests and shared expr use)
    if isinstance(node, (Compare, And, Or, Not)):
        return _cond_to_json(node)
    if isinstance(node, (Attr, Num, Str)):
        return _operand_to_json(node)
    raise TypeError(f"cannot serialize {type(node)}")



def from_json(d) -> object:
    """Convert a JSON dict (or JSON string) back to an AST node."""
    if isinstance(d, str):
        d = json.loads(d)
    op = d["op"]
    if op == "scan":
        return Relation(name=d["table"])
    if op == "select":
        return Select(cond=_cond_from_json(d["condition"]), input=from_json(d["child"]))
    if op == "project":
        return Project(attrs=list(d["columns"]), input=from_json(d["child"]))
    if op == "rename":
        return Rename(name=d["to"], input=from_json(d["child"]))
    if op == "distinct":
        return Distinct(input=from_json(d["child"]))
    if op in _JSON_TO_BIN:
        cond = None
        if "condition" in d:
            cond = _cond_from_json(d["condition"])
        return Binary(
            op=_JSON_TO_BIN[op],
            left=from_json(d["left"]),
            right=from_json(d["right"]),
            cond=cond,
        )
    if op in ("compare", "and", "or", "not"):
        return _cond_from_json(d)
    if op in ("attr", "num", "str"):
        return _operand_from_json(d)
    raise ValueError(f"unknown plan op: {op}")



def dumps(node, **kwargs) -> str:
    return json.dumps(to_json(node), **kwargs)


def loads(text: str):
    return from_json(json.loads(text))
