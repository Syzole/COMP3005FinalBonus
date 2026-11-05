"""Tuple comparisons and condition evaluation for the executor.

INT compared with VARCHAR is an error for every operator, including equality.
VARCHAR order is byte-wise on the UTF-8 encoding.
"""

from __future__ import annotations

from engine.expr import parse_condition
from engine.tree import And, Attr, Compare, Not, Num, Or, Str

from exec.budget import ExecError


def parse_cond(text: str):
    if not isinstance(text, str):
        raise ExecError("condition must be a string")
    try:
        return parse_condition(text)
    except Exception as e:
        raise ExecError(f"bad condition {text!r}: {e}") from e


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def resolve(name: str, schema: list) -> int:
    names = [col[0] for col in schema]
    exact = [i for i, n in enumerate(names) if n == name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ExecError(f"ambiguous column {name}")
    part = name.split(".")[-1]
    suffix = [i for i, n in enumerate(names) if n.split(".")[-1] == part]
    if len(suffix) == 1:
        return suffix[0]
    if len(suffix) > 1:
        raise ExecError(f"ambiguous column {name}")
    raise ExecError(f"unknown column {name}")


def column_type(schema: list, index: int):
    col = schema[index]
    if col[1] == "VARCHAR":
        return ("VARCHAR", col[2])
    return ("INT",)


def _value_of(operand, schema, row):
    if isinstance(operand, Num):
        return operand.value
    if isinstance(operand, Str):
        return _unquote(operand.value)
    if isinstance(operand, Attr):
        key = f"{operand.relation}.{operand.name}" if operand.relation else operand.name
        return row[resolve(key, schema)]
    raise ExecError(f"bad operand {operand}")


def _attr_name(operand) -> str | None:
    if isinstance(operand, Attr):
        if operand.relation:
            return f"{operand.relation}.{operand.name}"
        return operand.name
    return None


def check_condition_types(cond, schema, node_name: str) -> None:
    """Reject INT/VARCHAR comparisons once schemas are known."""
    if isinstance(cond, Compare):
        lt = _operand_type(cond.left, schema)
        rt = _operand_type(cond.right, schema)
        if lt is not None and rt is not None and lt[0] != rt[0]:
            raise ExecError(
                f"Node {node_name!r}: type error comparing {lt[0]} to {rt[0]}"
            )
        return
    if isinstance(cond, And) or isinstance(cond, Or):
        check_condition_types(cond.left, schema, node_name)
        check_condition_types(cond.right, schema, node_name)
        return
    if isinstance(cond, Not):
        check_condition_types(cond.cond, schema, node_name)


def _operand_type(operand, schema):
    if isinstance(operand, Num):
        return ("INT",)
    if isinstance(operand, Str):
        return ("VARCHAR",)
    if isinstance(operand, Attr):
        key = f"{operand.relation}.{operand.name}" if operand.relation else operand.name
        try:
            idx = resolve(key, schema)
        except ExecError:
            raise ExecError(f"unknown column {key}")
        return column_type(schema, idx)
    return None


def compare_values(left, op: str, right) -> bool:
    if isinstance(left, str) != isinstance(right, str) or isinstance(left, bool) or isinstance(right, bool):
        raise ExecError("type error comparing INT to VARCHAR")
    if not isinstance(left, (int, str)) or not isinstance(right, (int, str)):
        raise ExecError("type error comparing INT to VARCHAR")
    if type(left) is not type(right):
        raise ExecError("type error comparing INT to VARCHAR")
    if isinstance(left, str):
        lb = left.encode("utf-8")
        rb = right.encode("utf-8")
        return _cmp_ord(lb, op, rb)
    return _cmp_ord(left, op, right)


def _cmp_ord(left, op, right) -> bool:
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
    raise ExecError(f"unknown comparison {op}")


def matches(cond, schema, row) -> bool:
    if isinstance(cond, Compare):
        left = _value_of(cond.left, schema, row)
        right = _value_of(cond.right, schema, row)
        return compare_values(left, cond.op, right)
    if isinstance(cond, And):
        return matches(cond.left, schema, row) and matches(cond.right, schema, row)
    if isinstance(cond, Or):
        return matches(cond.left, schema, row) or matches(cond.right, schema, row)
    if isinstance(cond, Not):
        return not matches(cond.cond, schema, row)
    raise ExecError("bad condition")


def cmp_values(left, right) -> int:
    """Three-way compare. Raises on mixed types. VARCHAR is UTF-8 byte order."""
    if isinstance(left, str) != isinstance(right, str) or type(left) is not type(right):
        raise ExecError("type error comparing INT to VARCHAR")
    if isinstance(left, str):
        left = left.encode("utf-8")
        right = right.encode("utf-8")
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def make_key_cmp(schema, keys: list, counter):
    """keys: list of (index, 'asc'|'desc'). One counter bump per tuple-pair compare."""
    specs = []
    for name, direction in keys:
        specs.append((resolve(name, schema), direction))

    def cmp(a, b) -> int:
        counter.comparisons += 1
        for idx, direction in specs:
            c = cmp_values(a[idx], b[idx])
            if c == 0:
                continue
            if direction == "desc":
                c = -c
            return c
        return 0

    return cmp


def equality_keys(cond, left_schema, right_schema, node_name: str):
    """Split an equality (or conjunction of them) into (left_idx, right_idx) pairs."""
    parts = _and_list(cond)
    pairs = []
    for part in parts:
        if not isinstance(part, Compare) or part.op != "=":
            raise ExecError(f"Node {node_name!r}: equality condition required")
        ln = _attr_name(part.left)
        rn = _attr_name(part.right)
        if ln is None or rn is None:
            raise ExecError(f"Node {node_name!r}: equality condition required")
        left_hit = _try_resolve(ln, left_schema)
        right_hit = _try_resolve(rn, right_schema)
        if left_hit is None or right_hit is None:
            # swapped sides
            left_hit = _try_resolve(rn, left_schema)
            right_hit = _try_resolve(ln, right_schema)
        if left_hit is None or right_hit is None:
            raise ExecError(f"Node {node_name!r}: equality condition required")
        lt = column_type(left_schema, left_hit)
        rt = column_type(right_schema, right_hit)
        if lt[0] != rt[0]:
            raise ExecError(
                f"Node {node_name!r}: type error comparing {lt[0]} to {rt[0]}"
            )
        pairs.append((left_hit, right_hit))
    if not pairs:
        raise ExecError(f"Node {node_name!r}: equality condition required")
    return pairs


def _and_list(cond):
    if isinstance(cond, And):
        return _and_list(cond.left) + _and_list(cond.right)
    return [cond]


def _try_resolve(name, schema):
    try:
        return resolve(name, schema)
    except ExecError:
        return None


def project_key(row, indexes) -> tuple:
    return tuple(row[i] for i in indexes)


def fnv(values, salt: int) -> int:
    """Stable hash. Not Python's salted hash()."""
    h = (2166136261 ^ ((salt * 0x9E3779B9) & 0xFFFFFFFF)) & 0xFFFFFFFF
    for v in values:
        if isinstance(v, int):
            data = int(v).to_bytes(4, "little", signed=True)
        elif isinstance(v, str):
            data = v.encode("utf-8")
        else:
            raise ExecError("type error hashing aggregate or join key")
        for b in data:
            h ^= b
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= 0xFF
    return h
