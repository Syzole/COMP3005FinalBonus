from engine.table import Table
from engine.tree import *

"""
Evaluate an RA AST against a catalog.
Catalog is any mapping name -> relation source (objects with .attributes and .rows).
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
    result = set()
    for row in table.rows:
        result.add(tuple(row))
    return result


def from_set(attrs: list[str], rows: set[tuple]) -> Table:
    return Table(attributes=attrs, rows=[list(r) for r in rows])


def relation_label(node) -> str | None:
    if isinstance(node, Relation):
        return node.name
    if isinstance(node, Rename):
        return node.name
    return None


def qualify(table: Table, name: str | None) -> Table:
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


def _as_table(source) -> Table:
    """Accept Table or any object with attributes + rows."""
    if isinstance(source, Table):
        return source
    return Table(attributes=list(source.attributes), rows=[list(r) for r in source.rows])


def _lookup(catalog, name: str) -> Table:
    if callable(catalog):
        try:
            source = catalog(name)
        except Exception as e:
            raise EvalError(f"name, unknown relation {name}") from e
        if source is None:
            raise EvalError(f"name, unknown relation {name}")
        return _as_table(source)
    if name not in catalog:
        raise EvalError(f"name, unknown relation {name}")
    return _as_table(catalog[name])


def evaluate(node, catalog) -> Table:
    """Recursively evaluate AST. catalog: Mapping[str, Table-like] or callable(name)->Table-like."""
    global select_comparisons

    if isinstance(node, Relation):
        return _lookup(catalog, node.name)

    if isinstance(node, Select):
        child = evaluate(node.input, catalog)
        kept = []
        for row in child.rows:
            select_comparisons += 1
            if matches(node.cond, child.attributes, row):
                kept.append(row)
        return Table(attributes=child.attributes, rows=kept)

    if isinstance(node, Project):
        seen = set()
        for attr in node.attrs:
            if attr in seen:
                raise EvalError(f"schema, duplicate attribute {attr}")
            seen.add(attr)

        child = evaluate(node.input, catalog)
        for attr in node.attrs:
            if attr not in child.attributes:
                raise EvalError(f"name, unknown attribute {attr}")
            if child.attributes.count(attr) > 1:
                raise EvalError(f"schema, duplicate attribute {attr}")

        indexes = [child.attributes.index(a) for a in node.attrs]
        # Bag semantics: keep duplicate projected rows.
        rows = [[row[i] for i in indexes] for row in child.rows]
        return Table(attributes=list(node.attrs), rows=rows)

    if isinstance(node, Distinct):
        child = evaluate(node.input, catalog)
        rows = []
        seen = set()
        for row in child.rows:
            key = tuple(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(list(row))
        return Table(attributes=child.attributes, rows=rows)

    if isinstance(node, Binary):
        left = evaluate(node.left, catalog)
        right = evaluate(node.right, catalog)
        if node.op == "join":
            left = qualify(left, relation_label(node.left))
            right = qualify(right, relation_label(node.right))
        return eval_binary(node.op, left, right, node.cond)

    if isinstance(node, Rename):
        child = evaluate(node.input, catalog)
        attrs = [f"{node.name}.{a.split('.')[-1]}" for a in child.attributes]
        check_duplicate_attributes(attrs)
        return Table(attributes=attrs, rows=list(child.rows))

    raise ValueError(f"not implemented: {type(node)}")


def eval_binary(op, left, right, cond):
    global join_comparisons
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
        rows = [l + r for l in left.rows for r in right.rows]
        return Table(attributes=attrs, rows=rows)

    if op == "join":
        if cond is None:
            raise EvalError("join requires a condition")
        attrs = left.attributes + right.attributes
        check_duplicate_attributes(attrs)
        rows = []
        for l in left.rows:
            for r in right.rows:
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
        count = attrs.count(key)
        if count == 0:
            raise EvalError(f"name, unknown attribute {key}")
        if count > 1:
            raise EvalError(f"schema, ambiguous attribute {key}")
        return row[attrs.index(key)]
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
    for row in table.rows:
        cells = ", ".join(str(v) for v in row)
        lines.append(f"    {cells}")
    lines.append("}")
    return "\n".join(lines)
