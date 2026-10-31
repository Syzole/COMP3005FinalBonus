from __future__ import annotations

from engine.table import Table, Catalog, RelationSource, iter_tuples, materialize
from engine.tree import *

"""
Evaluate an RA AST against a catalog you pass in.

Catalog may be:
  - a Catalog instance
  - a Mapping[str, RelationSource]
  - a callable(name) -> RelationSource

Operators pull tuples via RelationSource / iter_tuples; they do not open files
or depend on a global in-memory store.
"""

join_comparisons = 0
select_comparisons = 0


def reset_counters():
    global join_comparisons, select_comparisons
    join_comparisons = 0
    select_comparisons = 0


class EvalError(Exception):
    pass


def as_set(table: Table) -> set[tuple]:
    return {tuple(row) for row in iter_tuples(table)}


def from_set(attrs: list[str], rows: set[tuple]) -> Table:
    return Table(attributes=attrs, rows=[list(r) for r in rows])


def relation_label(node) -> str | None:
    if isinstance(node, Relation):
        return node.name
    if isinstance(node, Rename):
        return node.name
    return None


def qualify(table: RelationSource, name: str | None) -> Table:
    """Prefix (or re-prefix) columns as name.column."""
    table = materialize(table)
    if name is None:
        return table
    attrs = [f"{name}.{a.split('.')[-1]}" for a in table.attributes]
    return Table(attributes=attrs, rows=[list(r) for r in table.rows])


def normalize(v):
    if isinstance(v, str) and len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def check_duplicate_attributes(attrs: list[str]):
    seen = set()
    for attr in attrs:
        if attr in seen:
            raise EvalError(f"schema, duplicate attribute {attr}")
        seen.add(attr)


def resolve_attr(key: str, attrs: list[str]) -> int:
    """
    Resolve a column reference against a schema.

    Supports table.column everywhere:
      - exact match on attrs
      - qualified key R.col against bare col (or unique *.col)
      - bare key col against unique R.col
    """
    exact = [i for i, a in enumerate(attrs) if a == key]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise EvalError(f"schema, ambiguous attribute {key}")

    if "." in key:
        _rel, col = key.rsplit(".", 1)
        # Bare column in schema
        bare = [i for i, a in enumerate(attrs) if a == col]
        if len(bare) == 1:
            return bare[0]
        if len(bare) > 1:
            raise EvalError(f"schema, ambiguous attribute {key}")
        # Unique suffix match (any qualification)
        suffix = [i for i, a in enumerate(attrs) if a.split(".")[-1] == col]
        if len(suffix) == 1:
            return suffix[0]
        if len(suffix) > 1:
            raise EvalError(f"schema, ambiguous attribute {key}")
        raise EvalError(f"name, unknown attribute {key}")

    # Bare key: unique attr whose column part equals key
    suffix = [i for i, a in enumerate(attrs) if a.split(".")[-1] == key]
    if len(suffix) == 1:
        return suffix[0]
    if len(suffix) > 1:
        raise EvalError(f"schema, ambiguous attribute {key}")
    raise EvalError(f"name, unknown attribute {key}")


def _open(catalog, name: str) -> RelationSource:
    """Pull a relation source from whatever catalog the caller passed in."""
    if isinstance(catalog, Catalog):
        source = catalog.get(name)
        if source is None:
            raise EvalError(f"name, unknown relation {name}")
        return source

    if callable(catalog) and not isinstance(catalog, dict):
        try:
            source = catalog(name)
        except EvalError:
            raise
        except Exception as e:
            raise EvalError(f"name, unknown relation {name}") from e
        if source is None:
            raise EvalError(f"name, unknown relation {name}")
        return source

    # Mapping-like (dict, etc.)
    try:
        if name not in catalog:
            raise EvalError(f"name, unknown relation {name}")
        return catalog[name]
    except EvalError:
        raise
    except Exception as e:
        raise EvalError(f"name, unknown relation {name}") from e


def evaluate(node, catalog) -> Table:
    """Recursively evaluate AST. catalog: Catalog | Mapping | callable(name)->source."""
    global select_comparisons

    if isinstance(node, Relation):
        # Catalog supplies the source; do not bake in a global table store.
        # Join/rename qualify columns; resolve_attr accepts table.column everywhere.
        return materialize(_open(catalog, node.name))

    if isinstance(node, Select):
        child = evaluate(node.input, catalog)
        kept = []
        for row in iter_tuples(child):
            select_comparisons += 1
            if matches(node.cond, child.attributes, row):
                kept.append(row)
        return Table(attributes=list(child.attributes), rows=kept)

    if isinstance(node, Project):
        seen = set()
        for attr in node.attrs:
            if attr in seen:
                raise EvalError(f"schema, duplicate attribute {attr}")
            seen.add(attr)

        child = evaluate(node.input, catalog)
        indexes = []
        for attr in node.attrs:
            indexes.append(resolve_attr(attr, child.attributes))

        # Bag semantics: keep duplicate projected rows.
        rows = [[row[i] for i in indexes] for row in iter_tuples(child)]
        # Output names as requested (may be bare or qualified).
        return Table(attributes=list(node.attrs), rows=rows)

    if isinstance(node, Distinct):
        child = evaluate(node.input, catalog)
        rows = []
        seen = set()
        for row in iter_tuples(child):
            key = tuple(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(list(row))
        return Table(attributes=list(child.attributes), rows=rows)

    if isinstance(node, Binary):
        left = evaluate(node.left, catalog)
        right = evaluate(node.right, catalog)
        if node.op == "join":
            # Re-qualify join sides from the plan labels (handles rename).
            left = qualify(left, relation_label(node.left))
            right = qualify(right, relation_label(node.right))
        return eval_binary(node.op, left, right, node.cond)

    if isinstance(node, Rename):
        child = evaluate(node.input, catalog)
        attrs = [f"{node.name}.{a.split('.')[-1]}" for a in child.attributes]
        check_duplicate_attributes(attrs)
        return Table(attributes=attrs, rows=[list(r) for r in iter_tuples(child)])

    raise ValueError(f"not implemented: {type(node)}")


def eval_binary(op, left, right, cond):
    global join_comparisons
    left = materialize(left)
    right = materialize(right)

    if op in ("union", "intersect", "minus"):
        if left.attributes != right.attributes:
            raise EvalError("schema, not union compatible")
        L, R = as_set(left), as_set(right)
        if op == "union":
            out = L | R
        elif op == "intersect":
            out = L & R
        else:
            out = L - R
        return Table(attributes=left.attributes, rows=[list(r) for r in out])

    if op == "times":
        attrs = left.attributes + right.attributes
        check_duplicate_attributes(attrs)
        rows = [l + r for l in iter_tuples(left) for r in iter_tuples(right)]
        return Table(attributes=attrs, rows=rows)

    if op == "join":
        if cond is None:
            raise EvalError("join requires a condition")
        attrs = left.attributes + right.attributes
        check_duplicate_attributes(attrs)
        rows = []
        for l in iter_tuples(left):
            for r in iter_tuples(right):
                join_comparisons += 1
                row = l + r
                if matches(cond, attrs, row):
                    rows.append(row)
        return Table(attributes=attrs, rows=rows)

    raise EvalError(f"not implemented: {op}")


def matches(cond, attrs: list[str], row: list) -> bool:
    if isinstance(cond, Compare):
        left = value_of(cond.left, attrs, row)
        right = value_of(cond.right, attrs, row)
        return compare(left, cond.op, right)
    if isinstance(cond, And):
        return matches(cond.left, attrs, row) and matches(cond.right, attrs, row)
    if isinstance(cond, Or):
        return matches(cond.left, attrs, row) or matches(cond.right, attrs, row)
    if isinstance(cond, Not):
        return not matches(cond.cond, attrs, row)
    raise ValueError(f"not implemented: {type(cond)}")


def value_of(operand, attrs, row):
    if isinstance(operand, Num):
        return operand.value
    if isinstance(operand, Str):
        s = operand.value
        if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
            s = s[1:-1].replace("''", "'")
        return s
    if isinstance(operand, Attr):
        key = f"{operand.relation}.{operand.name}" if operand.relation else operand.name
        return row[resolve_attr(key, attrs)]
    raise ValueError(f"not implemented: {type(operand)}")


def compare(left, op, right):
    left = normalize(left)
    right = normalize(right)
    if op in ("<", "<=", ">", ">=") and type(left) is not type(right):
        if isinstance(left, (int, float)) and isinstance(right, str) or \
           isinstance(right, (int, float)) and isinstance(left, str):
            raise EvalError("type, number compared to string")

    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise ValueError(op)


def format_result(table: Table) -> str:
    header = ", ".join(table.attributes)
    lines = [f"result ({header}) = {{"]
    for row in iter_tuples(table):
        cells = ", ".join(str(v) for v in row)
        lines.append(f"    {cells}")
    lines.append("}")
    return "\n".join(lines)
