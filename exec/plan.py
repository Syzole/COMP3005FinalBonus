"""Validate a physical plan and build an iterator tree."""

from __future__ import annotations

from exec.budget import ExecError
from exec.ops import (
    Distinct, Except, Filter, GroupBy, HashJoin, Limit, NestedLoopJoin,
    Project, SeqScan, Sort, SortMergeJoin, Union, UnionAll, Intersect,
)


def _build(node, where: str):
    if not isinstance(node, dict):
        raise ExecError(f"Node '{where}': operator must be an object.")
    if "op" not in node:
        raise ExecError(f"Node '{where}': missing required field 'op'.")
    op = node["op"]
    if not isinstance(op, str):
        raise ExecError(f"Node '{where}': field 'op' must be a string.")
    builder = _BUILDERS.get(op)
    if builder is None:
        raise ExecError(f"Node '{op}': unknown operator '{op}'.")
    return builder(node)


def _req(node, op, field):
    if field not in node:
        raise ExecError(f"Node '{op}': missing required field '{field}'.")
    return node[field]


def _object(value, op, field):
    if not isinstance(value, dict):
        raise ExecError(f"Node '{op}': field '{field}' must be an object.")
    return value


def _child(node, op):
    return _build(_object(_req(node, op, "child"), op, "child"), op)


def _side(node, op, field):
    return _build(_object(_req(node, op, field), op, field), op)


def _string(node, op, field):
    value = _req(node, op, field)
    if not isinstance(value, str):
        raise ExecError(f"Node '{op}': field '{field}' must be a string.")
    return value


def _keys(node, op):
    keys = _req(node, op, "keys")
    if not isinstance(keys, list) or not keys:
        raise ExecError(
            f"Node '{op}': field 'keys' must be a list of [column, direction] pairs."
        )
    parsed = []
    for item in keys:
        if (
            not isinstance(item, list) or len(item) != 2
            or not isinstance(item[0], str) or item[1] not in ("asc", "desc")
        ):
            raise ExecError(
                f"Node '{op}': field 'keys' must be a list of [column, direction] pairs."
            )
        parsed.append((item[0], item[1]))
    return parsed


def _mode(node, op):
    mode = _req(node, op, "mode")
    if mode not in ("sort", "hash"):
        raise ExecError(f"Node '{op}': field 'mode' must be 'sort' or 'hash'.")
    return mode


def _seqscan(node):
    # Optional "as" → column quals use alias (e.did), not Emp.did.
    table = _string(node, "seqscan", "table")
    alias = None
    if "as" in node:
        if not isinstance(node["as"], str):
            raise ExecError("Node 'seqscan': field 'as' must be a string.")
        alias = node["as"]
    return ("seqscan", table, alias)


def _filter(node):
    return ("filter", _string(node, "filter", "condition"), _child(node, "filter"))


def _project(node):
    columns = _req(node, "project", "columns")
    if not isinstance(columns, list) or not columns or any(not isinstance(c, str) for c in columns):
        raise ExecError("Node 'project': field 'columns' must be a list of strings.")
    return ("project", columns, _child(node, "project"))


def _sort(node):
    return ("sort", _keys(node, "sort"), _child(node, "sort"))


def _distinct(node):
    return ("distinct", _mode(node, "distinct"), _child(node, "distinct"))


def _group(node):
    mode = _mode(node, "group_by")
    keys = _req(node, "group_by", "keys")
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
        raise ExecError("Node 'group_by': field 'keys' must be a list of column names.")
    aggs = _req(node, "group_by", "aggregates")
    if not isinstance(aggs, list) or not aggs:
        raise ExecError("Node 'group_by': field 'aggregates' must be a list.")
    for item in aggs:
        # Accept [fn, arg] or [fn, arg, out_name] (M3/M4 emit the alias form).
        if (
            not isinstance(item, list)
            or len(item) not in (2, 3)
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or (len(item) == 3 and not isinstance(item[2], str))
        ):
            raise ExecError(
                "Node 'group_by': field 'aggregates' must be a list of "
                "[function, argument] or [function, argument, out_name] triples."
            )
        if item[0] not in ("count", "sum", "avg", "min", "max"):
            raise ExecError(f"Node 'group_by': unknown aggregate '{item[0]}'.")
    return ("group_by", mode, keys, aggs, _child(node, "group_by"))


def _join(kind):
    def build(node):
        return (
            kind,
            _string(node, kind, "condition"),
            _side(node, kind, "left"),
            _side(node, kind, "right"),
        )
    return build


def _binary(kind):
    def build(node):
        return (kind, _side(node, kind, "left"), _side(node, kind, "right"))
    return build


def _limit(node):
    if "n" not in node:
        raise ExecError("Node 'limit': missing required field 'n'.")
    n = node["n"]
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ExecError("Node 'limit': field 'n' must be a non-negative integer.")
    return ("limit", n, _child(node, "limit"))


# Validation produces a plain tuple tree so a bad node is reported before any
# operator is constructed. assemble() turns that tree into operators.
_BUILDERS = {
    "seqscan": _seqscan,
    "filter": _filter,
    "project": _project,
    "sort": _sort,
    "distinct": _distinct,
    "group_by": _group,
    "nested_loop_join": _join("nested_loop_join"),
    "sort_merge_join": _join("sort_merge_join"),
    "hash_join": _join("hash_join"),
    "union_all": _binary("union_all"),
    "union": _binary("union"),
    "intersect": _binary("intersect"),
    "except": _binary("except"),
    "limit": _limit,
}


def build_plan(node, ctx):
    spec = _build(node, "plan")
    return _assemble(spec, ctx)


def _assemble(spec, ctx):
    kind = spec[0]
    if kind == "seqscan":
        # spec = ("seqscan", table, alias|None)
        return SeqScan(ctx, spec[1], alias=spec[2] if len(spec) > 2 else None)
    if kind == "filter":
        return Filter(ctx, spec[1], _assemble(spec[2], ctx))
    if kind == "project":
        return Project(ctx, spec[1], _assemble(spec[2], ctx))
    if kind == "sort":
        return Sort(ctx, _assemble(spec[2], ctx), spec[1])
    if kind == "distinct":
        return Distinct(ctx, spec[1], _assemble(spec[2], ctx))
    if kind == "group_by":
        return GroupBy(ctx, spec[1], spec[2], spec[3], _assemble(spec[4], ctx))
    if kind == "nested_loop_join":
        return NestedLoopJoin(ctx, spec[1], _assemble(spec[2], ctx), _assemble(spec[3], ctx))
    if kind == "sort_merge_join":
        return SortMergeJoin(ctx, spec[1], _assemble(spec[2], ctx), _assemble(spec[3], ctx))
    if kind == "hash_join":
        return HashJoin(ctx, spec[1], _assemble(spec[2], ctx), _assemble(spec[3], ctx))
    if kind == "union_all":
        return UnionAll(ctx, _assemble(spec[1], ctx), _assemble(spec[2], ctx))
    if kind == "union":
        return Union(ctx, _assemble(spec[1], ctx), _assemble(spec[2], ctx))
    if kind == "intersect":
        return Intersect(ctx, _assemble(spec[1], ctx), _assemble(spec[2], ctx))
    if kind == "except":
        return Except(ctx, _assemble(spec[1], ctx), _assemble(spec[2], ctx))
    if kind == "limit":
        return Limit(ctx, spec[1], _assemble(spec[2], ctx))
    raise ExecError(f"Node '{kind}': unknown operator '{kind}'.")
