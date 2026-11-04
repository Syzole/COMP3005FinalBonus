"""Physical plan JSON validation and loading."""

from __future__ import annotations

import json
from typing import Any


class PlanError(Exception):
    """Plan does not conform to the physical schema."""


UNARY = {
    "filter", "project", "sort", "distinct", "group_by",
    "limit", "union_all",  # union_all is binary — see BINARY
}

# shape of the plan tree
BINARY = {
    "nested_loop_join", "sort_merge_join", "hash_join",
    "union_all", "union", "intersect", "except",
}
LEAF = {"seqscan"}

REQUIRED = { #required fields for each operator
    "seqscan": ["table"],
    "filter": ["condition", "child"],
    "project": ["columns", "child"],
    "distinct": ["mode", "child"],
    "sort": ["keys", "child"],
    "group_by": ["mode", "keys", "aggregates", "child"],
    "nested_loop_join": ["condition", "left", "right"],
    "sort_merge_join": ["condition", "left", "right"],
    "hash_join": ["condition", "left", "right"],
    "union_all": ["left", "right"],
    "union": ["left", "right"],
    "intersect": ["left", "right"],
    "except": ["left", "right"],
    "limit": ["n", "child"],
}


def loads(text: str) -> dict: # parse json into a dict
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanError(f"malformed JSON: {e.msg} at line {e.lineno} col {e.colno}") from e
    if not isinstance(obj, dict):
        raise PlanError("plan root must be a JSON object")
    validate(obj)
    return obj


def load_file(path) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        return loads(f.read())


def validate(node: Any, path: str = "root") -> None: # validate the plan tree
    if not isinstance(node, dict):
        raise PlanError(f"Node at {path}: expected object, got {type(node).__name__}")
    if "op" not in node:
        raise PlanError(f"Node at {path}: missing required field 'op'.")
    op = node["op"]
    if not isinstance(op, str):
        raise PlanError(f"Node at {path}: field 'op' must be a string.")
    if op not in REQUIRED:
        raise PlanError(f"Node '{op}': unknown operator '{op}'.")

    for field in REQUIRED[op]:
        if field not in node:
            raise PlanError(f"Node '{op}': missing required field '{field}'.")

    if op == "seqscan":
        if not isinstance(node["table"], str):
            raise PlanError(f"Node 'seqscan': field 'table' must be a string.")
        if "as" in node and not isinstance(node["as"], str):
            raise PlanError(f"Node 'seqscan': field 'as' must be a string.")
    elif op == "filter":
        if not isinstance(node["condition"], str):
            raise PlanError(f"Node 'filter': field 'condition' must be a string.")
        validate(node["child"], f"{path}/child")
    elif op == "project":
        cols = node["columns"]
        if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
            raise PlanError(f"Node 'project': field 'columns' must be a list of strings.")
        validate(node["child"], f"{path}/child")
    elif op == "distinct":
        if node["mode"] not in ("sort", "hash"):
            raise PlanError(f"Node 'distinct': field 'mode' must be 'sort' or 'hash'.")
        validate(node["child"], f"{path}/child")
    elif op == "sort":
        keys = node["keys"]
        if not isinstance(keys, list) or not keys:
            raise PlanError(f"Node 'sort': field 'keys' must be a non-empty list.")
        for k in keys:
            if (
                not isinstance(k, list)
                or len(k) != 2
                or not isinstance(k[0], str)
                or k[1] not in ("asc", "desc")
            ):
                raise PlanError(
                    f"Node 'sort': each key must be [column, 'asc'|'desc']."
                )
        validate(node["child"], f"{path}/child")
    elif op == "group_by":
        if node["mode"] not in ("sort", "hash"):
            raise PlanError(f"Node 'group_by': field 'mode' must be 'sort' or 'hash'.")
        if not isinstance(node["keys"], list) or not all(
            isinstance(k, str) for k in node["keys"]
        ):
            raise PlanError(f"Node 'group_by': field 'keys' must be a list of strings.")
        aggs = node["aggregates"]
        if not isinstance(aggs, list):
            raise PlanError(f"Node 'group_by': field 'aggregates' must be a list.")
        for a in aggs:
            if (
                not isinstance(a, list)
                or len(a) not in (2, 3)
                or not isinstance(a[0], str)
                or not isinstance(a[1], str)
                or (len(a) == 3 and not isinstance(a[2], str))
            ):
                raise PlanError(
                    f"Node 'group_by': each aggregate must be [fn, column_or_*] "
                    f"or [fn, column_or_*, out_name]."
                )
            if a[0].lower() not in ("count", "sum", "avg", "min", "max"):
                raise PlanError(
                    f"Node 'group_by': unknown aggregate '{a[0]}'."
                )
        validate(node["child"], f"{path}/child")
    elif op in ("nested_loop_join", "sort_merge_join", "hash_join"):
        if not isinstance(node["condition"], str):
            raise PlanError(f"Node '{op}': field 'condition' must be a string.")
        validate(node["left"], f"{path}/left")
        validate(node["right"], f"{path}/right")
    elif op in ("union_all", "union", "intersect", "except"):
        validate(node["left"], f"{path}/left")
        validate(node["right"], f"{path}/right")
    elif op == "limit":
        if not isinstance(node["n"], int) or node["n"] < 0:
            raise PlanError(f"Node 'limit': field 'n' must be a non-negative integer.")
        validate(node["child"], f"{path}/child")
