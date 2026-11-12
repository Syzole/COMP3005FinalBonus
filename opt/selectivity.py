"""Selectivity estimation (M4spec §2 defaults)."""

from __future__ import annotations

import math

from engine.expr import parse_condition
from engine.tree import And, Attr, Compare, Not, Num, Or, Str
from opt.stats import col_stats


def selectivity(condition: str, stats_map: dict, n_tuples: int) -> float:
    """Estimate sel of a condition string against available table stats.

    stats_map: qualified-or-table name -> stats dict
    """
    # reuse engine parser (no re)
    try:
        node = parse_condition(condition.strip())
    except Exception:
        return 0.1
    return _sel_node(node, stats_map, n_tuples)


def _sel_node(node, stats_map: dict, n_tuples: int) -> float:
    # OR: sel = s1+s2-s1*s2
    if isinstance(node, Or):
        s1 = _sel_node(node.left, stats_map, n_tuples)
        s2 = _sel_node(node.right, stats_map, n_tuples)
        return max(0.0, min(1.0, s1 + s2 - s1 * s2))

    # AND: product of sels
    if isinstance(node, And):
        s1 = _sel_node(node.left, stats_map, n_tuples)
        s2 = _sel_node(node.right, stats_map, n_tuples)
        return max(0.0, min(1.0, s1 * s2))

    # NOT: complement
    if isinstance(node, Not):
        return 1.0 - _sel_node(node.cond, stats_map, n_tuples)

    # atomic compare
    if isinstance(node, Compare):
        return _atomic_sel(node, stats_map, n_tuples)

    return 0.1


def _attr_name(node: Attr) -> str:
    # Emp.DID or plain col
    if node.relation:
        return f"{node.relation}.{node.name}"
    return node.name


def _lit_value(node):
    # Num / Str / Attr-as-raw
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        s = node.value
        # lexer keeps quotes
        if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
            return s[1:-1].replace("''", "'")
        return s
    if isinstance(node, Attr):
        return _attr_name(node)
    return None


def _atomic_sel(node: Compare, stats_map: dict, n_tuples: int) -> float:
    if not isinstance(node.left, Attr):
        return 0.1
    col = _attr_name(node.left)
    op = "!=" if node.op == "<>" else node.op
    val = _lit_value(node.right)
    st = _find_stats(col, stats_map)
    cs = col_stats(st, col)
    ndv = max(1, cs.get("ndv") or 1)
    n = max(1, (st or {}).get("n_tuples") or n_tuples or 1)

    if op == "=":
        # hist: count/(n*ndv_bucket); else 1/ndv
        hist = cs.get("hist") or []
        if hist and val is not None:
            for b in hist:
                lo, hi = b["lo"], b["hi"]
                if _le(lo, val) and _le(val, hi):
                    bndv = max(1, b.get("ndv") or 1)
                    return max(1e-9, b["count"] / (n * bndv))
        return 1.0 / ndv
    if op == "!=":
        eq = Compare(op="=", left=node.left, right=node.right)
        return 1.0 - _atomic_sel(eq, stats_map, n_tuples)

    # < / > : hist fractions, else uniform min/max, else 0.33
    hist = cs.get("hist") or []
    vmin, vmax = cs.get("min"), cs.get("max")
    if hist and val is not None:
        return _range_sel_hist(hist, n, op, val)
    if vmin is not None and vmax is not None and val is not None and vmin != vmax:
        try:
            span = float(vmax) - float(vmin)
            if op == "<":
                return max(0.0, min(1.0, (float(val) - float(vmin)) / span))
            if op == ">":
                return max(0.0, min(1.0, (float(vmax) - float(val)) / span))
            if op == "<=":
                return max(0.0, min(1.0, (float(val) - float(vmin)) / span))
            if op == ">=":
                return max(0.0, min(1.0, (float(vmax) - float(val)) / span))
        except (TypeError, ValueError):
            pass
    return 0.33


def _range_sel_hist(hist, n, op, val) -> float:
    """Whole buckets on one side + linear fraction of the bucket holding val."""
    kept = 0.0
    for b in hist:
        lo, hi, cnt = b["lo"], b["hi"], b["count"]
        if op in ("<", "<="):
            if _lt(hi, val) or (op == "<=" and hi == val):
                kept += cnt
            elif _le(lo, val) and _le(val, hi):
                # fraction inside bucket
                try:
                    span = float(hi) - float(lo)
                    if span <= 0:
                        frac = 0.5
                    else:
                        frac = (float(val) - float(lo)) / span
                        if op != "<":
                            frac = min(1.0, frac)
                    kept += cnt * max(0.0, min(1.0, frac))
                except (TypeError, ValueError):
                    kept += cnt * 0.5
        elif op in (">", ">="):
            if _gt(lo, val) or (op == ">=" and lo == val):
                kept += cnt
            elif _le(lo, val) and _le(val, hi):
                try:
                    span = float(hi) - float(lo)
                    if span <= 0:
                        frac = 0.5
                    else:
                        frac = (float(hi) - float(val)) / span
                    kept += cnt * max(0.0, min(1.0, frac))
                except (TypeError, ValueError):
                    kept += cnt * 0.5
    return max(0.0, min(1.0, kept / max(1, n)))


def _le(a, b):
    try:
        return a <= b
    except TypeError:
        return str(a) <= str(b)


def _lt(a, b):
    try:
        return a < b
    except TypeError:
        return str(a) < str(b)


def _gt(a, b):
    try:
        return a > b
    except TypeError:
        return str(a) > str(b)


def _find_stats(col: str, stats_map: dict):
    # stats_map keys are table names
    if "." in col:
        t = col.split(".", 1)[0]
        # alias may not match table — try all
        if t in stats_map:
            return stats_map[t]
    for st in stats_map.values():
        if st and col_stats(st, col).get("ndv"):
            return st
    return next(iter(stats_map.values()), None) if stats_map else None


def join_cardinality(n_r: int, n_s: int, cond: str, stats_map: dict) -> int:
    """Equality: nR*nS/max(ndv); else sel 0.1."""
    try:
        node = parse_condition(cond.strip())
    except Exception:
        return max(1, math.ceil(n_r * n_s * 0.1))

    if (
        isinstance(node, Compare)
        and node.op == "="
        and isinstance(node.left, Attr)
        and isinstance(node.right, Attr)
    ):
        left = _attr_name(node.left)
        right = _attr_name(node.right)
        ndv_l = max(1, col_stats(_find_stats(left, stats_map), left).get("ndv") or 1)
        ndv_r = max(1, col_stats(_find_stats(right, stats_map), right).get("ndv") or 1)
        return max(1, math.ceil(n_r * n_s / max(ndv_l, ndv_r)))

    return max(1, math.ceil(n_r * n_s * 0.1))


def est_pages(rows: int, width: float) -> int:
    """ceil(rows * width / 4096), at least 1 if any rows."""
    if rows <= 0:
        return 0
    return max(1, math.ceil(rows * max(width, 1.0) / 4096))
