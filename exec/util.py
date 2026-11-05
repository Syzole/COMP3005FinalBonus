"""Shared helpers for operators: schemas, keys, comparisons.

Schemas are lists of [qualified_name, type, ...] e.g. ["R.a", "INT"].
Qualified names (Table.col) are required so joins don't confuse columns on "id".
"""

from __future__ import annotations

from engine.expr import parse_condition, EvalError
from engine.tree import Compare, Attr, And, Or, Not
from exec.iterator import ExecError


def attr_names(schema: list) -> list[str]: #col name strings - used by matches() and index lookup
    return [c[0] for c in schema]


def qualify_schema(table: str, schema: list) -> list: # Turn 'a' into 'R.a' when loading from the heap/catalog
    out = []
    for col in schema:
        name = col[0]
        if "." in name:
            out.append(list(col))
        else:
            out.append([f"{table}.{name}"] + list(col[1:]))
    return out


def merge_schemas(left: list, right: list) -> list: #join schemas, starting with left, then right, if we see a duplicate, raise an error
    names = attr_names(left) + attr_names(right)
    seen = set()
    for n in names:
        if n in seen:
            raise ExecError(f"duplicate qualified column {n!r} in join schema")
        seen.add(n)
    return [list(c) for c in left] + [list(c) for c in right]


def check_set_compatible(left: list, right: list) -> None: #union/except/intersect need same arity and type
    if len(left) != len(right):
        raise ExecError(
            f"set op arity mismatch: left has {len(left)} cols, right has {len(right)}"
        )
    for i, (a, b) in enumerate(zip(left, right)):
        if a[1] != b[1]:
            raise ExecError(
                f"set op type mismatch at column {i}: {a[1]} vs {b[1]}"
            )
        if a[1] == "VARCHAR" and a[2] != b[2]:
            raise ExecError(
                f"set op VARCHAR length mismatch at column {i}: {a[2]} vs {b[2]}"
            )


def index_of(schema: list, name: str) -> int:
    names = attr_names(schema)
    if name not in names:
        raise ExecError(f"unknown column {name!r}")
    if names.count(name) > 1:
        raise ExecError(f"ambiguous column {name!r}")
    return names.index(name)


def extract_eq_keys(condition: str, left_schema: list, right_schema: list): 
    """Parse equality join condition into (left_key_indices, right_key_indices).
    
    Ex: Take 'R.b = S.b AND R.a = S.a' and turn them into index lists for hash
    """
    node = parse_condition(condition)
    pairs = _eq_pairs(node)
    if not pairs:
        raise ExecError(f"join condition is not equality: {condition!r}")
    left_names = attr_names(left_schema)
    right_names = attr_names(right_schema)
    li, ri = [], []
    for la, ra in pairs:
        lkey = _attr_key(la)
        rkey = _attr_key(ra)
        if lkey in left_names and rkey in right_names:
            li.append(left_names.index(lkey))
            ri.append(right_names.index(rkey))
        elif lkey in right_names and rkey in left_names:
            li.append(left_names.index(rkey))
            ri.append(right_names.index(lkey))
        else:
            raise ExecError(
                f"join keys {lkey!r}/{rkey!r} not found in left/right schemas"
            )
    return li, ri


def _attr_key(node) -> str:
    if isinstance(node, Attr):
        return f"{node.relation}.{node.name}" if node.relation else node.name
    raise ExecError("join key must be a column reference")


def _eq_pairs(node) -> list[tuple]:
    if isinstance(node, Compare):
        if node.op != "=":
            raise ExecError(f"hash/sort-merge join requires equality, got {node.op}")
        return [(node.left, node.right)]
    if isinstance(node, And):
        return _eq_pairs(node.left) + _eq_pairs(node.right)
    if isinstance(node, (Or, Not)):
        raise ExecError("join condition may only be equality ANDed together")
    raise ExecError(f"unsupported join condition node {type(node)}")


def key_tuple(row: list, idxs: list[int]) -> tuple: # Project a row to the join/sort key as a tuple
    return tuple(row[i] for i in idxs)


def cmp_keys(a: tuple, b: tuple, directions: list[str] | None = None) -> int: # Compare two keys, handling direction and type mismatches
    """Return -1, 0, 1. directions: 'asc'/'desc' per key; None = all asc."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x == y:
            continue
        if type(x) is not type(y):
            raise EvalError("type, number compared to string")
        less = x < y
        d = directions[i] if directions else "asc"
        if d == "asc":
            return -1 if less else 1
        return 1 if less else -1
    return 0


def compare_rows(a: list, b: list) -> int: # full tuple order for distinct / set operations
    """Lexicographic compare of full tuples (for distinct / set ops)."""
    for x, y in zip(a, b):
        if type(x) is not type(y):
            raise ExecError("type mismatch comparing tuples")
        if x < y:
            return -1
        if x > y:
            return 1
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return 0
