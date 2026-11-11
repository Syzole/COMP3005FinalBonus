"""Logical plan JSON helpers."""

from __future__ import annotations

import json


def dumps(plan: dict) -> str:
    # Pretty-print a logical plan dict
    return json.dumps(plan, indent=2)


def loads(text: str) -> dict:
    return json.loads(text)


def explain_tree(node, depth: int = 0) -> list[str]:
    """Indented tree for sql parse --explain."""
    pad = "  " * depth
    op = node.get("op", "?")
    # One-line summary per operator; recurse into child / left / right.
    if op == "scan":
        line = f"{pad}scan {node.get('table')}"
    elif op == "rename":
        line = f"{pad}rename {node.get('to')}"
    elif op == "select":
        line = f"{pad}select [{node.get('condition')}]"
    elif op == "project":
        cols = ",".join(node.get("columns", []))
        line = f"{pad}project [{cols}]"
    elif op == "join":
        line = f"{pad}join [{node.get('condition')}]"
    elif op == "cross":
        line = f"{pad}cross"
    elif op == "group_by":
        line = f"{pad}group_by keys={node.get('keys')} aggs={node.get('aggregates')}"
    elif op == "distinct":
        line = f"{pad}distinct"
    elif op == "sort":
        line = f"{pad}sort {node.get('keys')}"
    elif op == "limit":
        line = f"{pad}limit {node.get('n')}"
    elif op in ("union", "union_all", "intersect", "except"):
        line = f"{pad}{op}"
    else:
        line = f"{pad}{op}"  # unknown / future ops
    lines = [line]
    # Unary plans use "child"; binary set-ops / joins use left+right.
    if "child" in node:
        lines.extend(explain_tree(node["child"], depth + 1))
    if "left" in node:
        lines.extend(explain_tree(node["left"], depth + 1))
    if "right" in node:
        lines.extend(explain_tree(node["right"], depth + 1))
    return lines
