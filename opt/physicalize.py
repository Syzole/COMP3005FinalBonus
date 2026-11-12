"""Logical → physical plan (M4).

Pipeline:
  1. rewrite(logical)     — rules 1–7 push selects / turn cross→join / etc.
  2. load_stats_map(...)  — per-table n_tuples, n_pages, hist from catalog
  3. _phys(...)           — walk the rewritten tree bottom-up:
       logical op  →  physical op + est_rows / est_cost annotations

Physical names match M2 exec (seqscan, filter, nested_loop_join, …).
Join algorithm is picked by opt.cost.choose_join from page sizes + buffer B.
Cardinality / selectivity come from opt.selectivity (engine condition AST).

Rename is not a physical op: we fold the alias onto seqscan["as"] so M2
sees Emp.DID style names without an extra operator.
"""

from __future__ import annotations

import copy
import math

from engine.expr import parse_condition
from engine.tree import Attr, Compare
from opt.cost import choose_join, cost_seqscan, cost_sort
from opt.selectivity import selectivity, join_cardinality, est_pages
from opt.stats import unpack_stats, empty_defaults
from opt.dp import RelInfo, optimize_joins
from opt.rewrite import rewrite


def load_stats_map(catalog) -> dict:
    """table_name → stats dict used by selectivity / join_cardinality.

    Prefer ANALYZE blobs in the catalog; if a table was never analyzed,
    fall back to empty_defaults so costing still has something to chew on.
    """
    out = {}
    for name in catalog.list_tables():
        info = catalog.get_table(name)
        st = unpack_stats(info.get("stats") or b"", catalog.db_dir, name)
        if st is None:
            st = empty_defaults(info.get("num_tuples") or 1000, info.get("num_pages") or 10)
            st["n_tuples"] = info.get("num_tuples") or st["n_tuples"]
            st["n_pages"] = info.get("num_pages") or st["n_pages"]
        out[name] = st
    return out


def plan_physical(logical: dict, catalog, B: int = 32) -> dict:
    """Rewrite then physicalize; return the physical plan dict only."""
    rewritten = rewrite(logical)
    stats_map = load_stats_map(catalog)
    phys, _meta = _phys(rewritten, stats_map, B, catalog)
    return phys


def explain_physical(logical: dict, catalog, B: int = 32) -> tuple[dict, dict, list[str]]:
    """Same as plan_physical, plus a before-rewrite copy and indented explain lines.

    Returns (before_rewrite, physical, explain_lines).
    """
    before = copy.deepcopy(logical)
    rewritten = rewrite(logical)
    stats_map = load_stats_map(catalog)
    phys, annot = _phys(rewritten, stats_map, B, catalog)
    lines = _fmt_explain(phys, annot)
    return before, phys, lines


def _phys(n: dict, stats_map: dict, B: int, catalog) -> tuple[dict, dict]:
    """Bottom-up conversion of one logical tree.

    Returns (physical_root, annot) where annot[id(node)] holds
    {est_rows, est_cost, label} for explain output.

    walk() returns (physical_subtree, rows, pages, cost_so_far):
      rows/pages  — estimated size of this subtree's output
      cost_so_far — cumulative I/O from leaves up through this node
    """
    annot: dict = {}

    def walk(node: dict) -> tuple[dict, int, float, float]:
        op = node["op"]

        # --- leaves ---------------------------------------------------------
        if op == "scan":
            # logical scan → physical seqscan; cost = read all pages once
            table = node["table"]
            st = stats_map.get(table) or empty_defaults()
            tree = {"op": "seqscan", "table": table}
            rows, pages = st["n_tuples"], st["n_pages"]
            cost = cost_seqscan(pages)
            annot[id(tree)] = {"est_rows": rows, "est_cost": cost, "label": f"seqscan {table}"}
            return tree, rows, pages, cost

        # --- unary ----------------------------------------------------------
        if op == "rename":
            # not a physical op: push alias down onto the seqscan
            child_p, rows, pages, cost = walk(node["child"])
            child_p = _apply_alias(child_p, node["to"])
            return child_p, rows, pages, cost

        if op == "select":
            # logical select → filter; shrink row estimate by selectivity
            child_p, rows, pages, cost = walk(node["child"])
            sel = selectivity(node["condition"], stats_map, rows)
            out_rows = max(1, math.ceil(rows * sel)) if rows else 0
            tree = {"op": "filter", "condition": node["condition"], "child": child_p}
            annot[id(tree)] = {
                "est_rows": out_rows,
                "est_cost": 0,  # filter itself is CPU-only in this model
                "label": f"filter ({node['condition']})",
            }
            return tree, out_rows, est_pages(out_rows, 32), cost

        if op == "project":
            # column list only; row count unchanged, page width scales with cols
            child_p, rows, pages, cost = walk(node["child"])
            tree = {"op": "project", "columns": node["columns"], "child": child_p}
            width = max(8.0, 8.0 * len(node["columns"]))
            annot[id(tree)] = {"est_rows": rows, "est_cost": 0, "label": "project"}
            return tree, rows, est_pages(rows, width), cost

        # --- joins ----------------------------------------------------------
        if op == "join":
            # pick NLJ / SMJ / hash from page sizes; equality opens SMJ+hash
            left_p, lr, lp, lc = walk(node["left"])
            right_p, rr, rp, rc = walk(node["right"])
            # crude equality check: SMJ/hash need =, not !=
            equality = "=" in node["condition"] and "!=" not in node["condition"]
            jop, jcost = choose_join(lp, rp, B, equality=equality)
            rows = join_cardinality(lr, rr, node["condition"], stats_map)
            pages = est_pages(rows, 32)
            tree = {
                "op": jop,
                "condition": node["condition"],
                "left": left_p,
                "right": right_p,
            }
            annot[id(tree)] = {
                "est_rows": rows,
                "est_cost": jcost,
                "label": f"{jop} ({node['condition']})",
            }
            # total cost = children + this join's I/O
            return tree, rows, pages, lc + rc + jcost

        if op == "cross":
            # rewrite usually turns these into joins; if one survives, treat as
            # a join with true predicate (no equality → NLJ only)
            left_p, lr, lp, lc = walk(node["left"])
            right_p, rr, rp, rc = walk(node["right"])
            jop, jcost = choose_join(lp, rp, B, equality=False)
            rows = max(1, lr * rr)
            tree = {
                "op": jop,
                "condition": "1 = 1",
                "left": left_p,
                "right": right_p,
            }
            annot[id(tree)] = {"est_rows": rows, "est_cost": jcost, "label": jop}
            return tree, rows, est_pages(rows, 32), lc + rc + jcost

        # --- blocking / order ops -------------------------------------------
        if op == "group_by":
            child_p, rows, pages, cost = walk(node["child"])
            # hash if the input fits in B-1 frames; else external sort
            mode = "hash" if pages <= max(1, B - 1) else "sort"
            # M2 accepts [fn, arg] or [fn, arg, out_name]
            aggs = []
            for a in node.get("aggregates") or []:
                if len(a) >= 3:
                    aggs.append([a[0], a[1], a[2]])
                elif len(a) >= 2:
                    aggs.append([a[0], a[1]])
            tree = {
                "op": "group_by",
                "mode": mode,
                "keys": node.get("keys") or [],
                "aggregates": aggs,
                "child": child_p,
            }
            # rough group cardinality: 1 if no keys, else ~10% of input
            out_rows = max(1, rows // 10) if node.get("keys") else 1
            scost = cost_sort(pages, B) if mode == "sort" else float(pages)
            annot[id(tree)] = {"est_rows": out_rows, "est_cost": scost, "label": f"group_by/{mode}"}
            return tree, out_rows, est_pages(out_rows, 32), cost + scost

        if op == "distinct":
            child_p, rows, pages, cost = walk(node["child"])
            mode = "hash" if pages <= max(1, B - 1) else "sort"
            tree = {"op": "distinct", "mode": mode, "child": child_p}
            annot[id(tree)] = {
                "est_rows": rows,
                "est_cost": cost_sort(pages, B),
                "label": f"distinct/{mode}",
            }
            return tree, rows, pages, cost + cost_sort(pages, B)

        if op == "sort":
            child_p, rows, pages, cost = walk(node["child"])
            tree = {"op": "sort", "keys": node["keys"], "child": child_p}
            sc = cost_sort(pages, B)
            annot[id(tree)] = {"est_rows": rows, "est_cost": sc, "label": "sort"}
            return tree, rows, pages, cost + sc

        if op == "limit":
            child_p, rows, pages, cost = walk(node["child"])
            tree = {"op": "limit", "n": node["n"], "child": child_p}
            out = min(rows, node["n"]) if rows else 0
            annot[id(tree)] = {"est_rows": out, "est_cost": 0, "label": f"limit {node['n']}"}
            return tree, out, est_pages(out, 32), cost

        # --- set ops --------------------------------------------------------
        if op in ("union", "union_all", "intersect", "except"):
            left_p, lr, lp, lc = walk(node["left"])
            right_p, rr, rp, rc = walk(node["right"])
            tree = {"op": op, "left": left_p, "right": right_p}
            # union_all keeps duplicates; others approx as max(left, right)
            rows = lr + rr if op == "union_all" else max(lr, rr)
            annot[id(tree)] = {"est_rows": rows, "est_cost": 0, "label": op}
            return tree, rows, est_pages(rows, 32), lc + rc

        raise ValueError(f"unknown logical op {op}")

    phys, rows, pages, cost = walk(n)
    # ensure the root always has an annotation entry for explain
    annot[id(phys)] = annot.get(
        id(phys), {"est_rows": rows, "est_cost": cost, "label": phys.get("op")}
    )
    return phys, annot


def _apply_alias(node: dict, alias: str) -> dict:
    """Walk through projects/filters until we hit seqscan, then set as=alias.

    Logical:  rename(to=Emp, child=scan(Employee))
    Physical: seqscan(table=Employee, as=Emp)
    """
    node = dict(node)
    if node.get("op") == "seqscan":
        node["as"] = alias
        return node
    if "child" in node:
        node["child"] = _apply_alias(node["child"], alias)
    return node


def _fmt_explain(node: dict, annot: dict, depth: int = 0) -> list[str]:
    """Indent physical tree using annot labels (for `opt explain`)."""
    pad = "  " * depth
    meta = annot.get(id(node), {})
    label = meta.get("label", node.get("op", "?"))
    line = (
        f"{pad}{label:<40} "
        f"est_rows={meta.get('est_rows', '?')}   "
        f"est_cost={meta.get('est_cost', '?')}"
    )
    lines = [line]
    if "child" in node:
        lines.extend(_fmt_explain(node["child"], annot, depth + 1))
    if "left" in node:
        lines.extend(_fmt_explain(node["left"], annot, depth + 1))
    if "right" in node:
        lines.extend(_fmt_explain(node["right"], annot, depth + 1))
    return lines


def build_dp_for_joins(logical: dict, catalog, B: int):
    """Pull base relations + equality join edges out of a plan, then run DP.

    Used by the `opt dp-table` CLI. collect() walks the rewritten logical
    tree and builds:
      rels  — one RelInfo per scan (using rename alias as the relation id)
      edges — (left_alias, right_alias, cond_string) for each R.a = S.b join

    Condition sides come from engine parse_condition (Attr.relation), not regex.
    """
    rewritten = rewrite(logical)
    stats_map = load_stats_map(catalog)
    rels: list[RelInfo] = []
    edges: list[tuple[str, str, str]] = []

    def collect(n, alias=None):
        op = n["op"]
        if op == "scan":
            st = stats_map.get(n["table"]) or empty_defaults()
            # alias (from enclosing rename) becomes the DP relation id
            rid = alias or n["table"]
            rels.append(
                RelInfo(
                    rid, n["table"], alias, st["n_tuples"], st["n_pages"], st.get("width", 32), st
                )
            )
        elif op == "rename":
            collect(n["child"], alias=n["to"])
        elif op == "join":
            collect(n["left"])
            collect(n["right"])
            # need R.col = S.col so DP knows which two rels this edge connects
            try:
                cond = parse_condition(n["condition"].strip())
            except Exception:
                cond = None
            if (
                isinstance(cond, Compare)
                and cond.op == "="
                and isinstance(cond.left, Attr)
                and isinstance(cond.right, Attr)
                and cond.left.relation
                and cond.right.relation
            ):
                edges.append((cond.left.relation, cond.right.relation, n["condition"]))
        elif op == "cross":
            # no predicate → no DP edge; still collect both sides as rels
            collect(n["left"])
            collect(n["right"])
        elif "child" in n:
            collect(n["child"], alias)
        else:
            if "left" in n:
                collect(n["left"])
            if "right" in n:
                collect(n["right"])

    collect(rewritten)
    # same table may appear twice under different aliases; key by RelInfo.id
    uniq = {}
    for r in rels:
        uniq[r.id] = r
    rels = list(uniq.values())
    if len(rels) < 2:
        return None, {}
    cell, table = optimize_joins(rels, edges, B, stats_map)
    return cell, table
