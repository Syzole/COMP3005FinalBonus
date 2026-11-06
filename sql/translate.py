"""Translate resolved SQL AST -> logical plan dict (no optimization)."""

from __future__ import annotations

from sql import ast as A
from sql.condfmt import fmt_cond
from sql.errors import SqlError


def translate(q) -> dict:
    # high level : set op (union/intersect/except) or a single SELECT block
    if isinstance(q, A.SetOp):
        return {
            "op": q.op, # "union" | "union_all" | "intersect" | "except"
            "left": translate(q.left),
            "right": translate(q.right),
        }
    if isinstance(q, A.SelectBlock):
        return _translate_select(q)
    raise SqlError("syntax", f"cannot translate {type(q)}")


def _scan_or_rename(tr: A.TableRef) -> dict:
    if tr.kind == "subquery":
        child = translate(tr.subquery)
        return {"op": "rename", "to": tr.alias, "child": child}
    node = {"op": "scan", "table": tr.name}
    if tr.alias and tr.alias != tr.name:
        return {"op": "rename", "to": tr.alias, "child": node}
    return node


def _from_plan(from_clause) -> dict:
    # FROM: comma list -> nested cross; JOIN ON chain -> nested join.
    if isinstance(from_clause, A.FromComma):
        tables = from_clause.tables
        plan = _scan_or_rename(tables[0])
        for tr in tables[1:]:
            # left nested: ((t1 x t2) x t3) ...
            plan = {"op": "cross", "left": plan, "right": _scan_or_rename(tr)}
        return plan
    if isinstance(from_clause, A.JoinOn):
        plan = {
            "op": "join",
            "condition": fmt_cond(from_clause.condition),
            "left": _scan_or_rename(from_clause.left),
            "right": _scan_or_rename(from_clause.right),
        }
        for tr, cond in from_clause.more:
            # Further JOINs wrap the plan so far on the left
            plan = {
                "op": "join",
                "condition": fmt_cond(cond),
                "left": plan,
                "right": _scan_or_rename(tr),
            }
        return plan
    raise SqlError("syntax", "bad FROM")


def _translate_select(sel: A.SelectBlock) -> dict:
    #Build plan bottom up in sql eval order:
    #FROM -> WHERE -> GROUP BY -> HAVING -> PROJECT -> DISTINCT -> ORDER BY -> LIMIT
    plan = _from_plan(sel.from_clause)

    if sel.where is not None:
        plan = {"op": "select", "condition": fmt_cond(sel.where), "child": plan}

    # Aggregate if GROUP BY is present, or if SELECT has COUNT/SUM/etc
    if sel.group_by or any(
        isinstance(it.expr, A.Aggregate) for it in sel.items if it.expr != "*"
    ):
        keys = [g.resolved for g in sel.group_by]
        aggregates = []
        seen = set()
        for it in sel.items:
            if isinstance(it.expr, A.Aggregate):
                agg = it.expr
                arg = "*" if agg.arg == "*" else agg.arg.resolved
                # [function, arg, output name]
                triple = [agg.fn.lower(), arg, agg.out_name]
                key = (triple[0], triple[1], triple[2])
                if key not in seen:
                    seen.add(key)
                    aggregates.append(triple)
        # Having may mention aggregates that are not in the SELECT list.
        _collect_having_aggs(sel.having, aggregates, seen)
        plan = {
            "op": "group_by",
            "keys": keys,
            "aggregates": aggregates,
            "child": plan,
        }

    if sel.having is not None:
        # Filter after grouping; fmt_cond uses aggregate out_names (e.g. "n > 5").
        plan = {"op": "select", "condition": fmt_cond(sel.having), "child": plan}

    # project: choose output columns (SELECT * already expanded by resolve)
    if len(sel.items) == 1 and sel.items[0].expr == "*":
        columns = list(sel.star_expanded)
    else:
        columns = []
        for it in sel.items:
            if isinstance(it.expr, A.Aggregate):
                columns.append(it.expr.out_name)
            elif isinstance(it.expr, A.ColRef):
                columns.append(it.alias or it.expr.resolved)
    plan = {"op": "project", "columns": columns, "child": plan}

    aggregating = bool(sel.group_by) or any(
        isinstance(it.expr, A.Aggregate) for it in sel.items if it.expr != "*"
    )
    # Groups are already unique, so DISTINCT is redundant when aggregating.
    if sel.distinct and not aggregating:
        plan = {"op": "distinct", "child": plan}

    if sel.order_by:
        keys = []
        for sk in sel.order_by:
            keys.append([sk.resolved, sk.direction]) # ["t.a", "asc"|"desc"]
        plan = {"op": "sort", "keys": keys, "child": plan}

    if sel.limit is not None:
        plan = {"op": "limit", "n": sel.limit, "child": plan}

    return plan


def _collect_having_aggs(cond, aggregates: list, seen: set):
    # Walk HAVING and append any aggregate not already listed for group_by.
    if cond is None:
        return
    if isinstance(cond, (A.And, A.Or)):
        _collect_having_aggs(cond.left, aggregates, seen)
        _collect_having_aggs(cond.right, aggregates, seen)
        return
    if isinstance(cond, A.Not):
        _collect_having_aggs(cond.cond, aggregates, seen)
        return
    if isinstance(cond, A.Compare):
        for side in (cond.left, cond.right):
            if isinstance(side, A.Aggregate):
                arg = "*" if side.arg == "*" else side.arg.resolved
                triple = [side.fn.lower(), arg, side.out_name]
                key = (triple[0], triple[1], triple[2])
                #  Same fn+arg already in select list — skip duplicate
                if any(a[0] == triple[0] and a[1] == triple[1] for a in aggregates):
                    continue
                if key not in seen:
                    seen.add(key)
                    aggregates.append(triple)
