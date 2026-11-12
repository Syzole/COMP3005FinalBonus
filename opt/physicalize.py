"""Logical → physical plan (M4).

Pipeline:
  1. rewrite(logical)     — rules 1–7 push selects / turn cross→join / etc.
  2. load_stats_map(...)  — per-table n_tuples, n_pages, hist from catalog
  3. _phys(...)           — walk the rewritten tree bottom-up:
       multi-relation join clusters are replaced by Selinger left-deep DP
       (order + algorithm); other ops map 1:1 to physical names.

Physical names match M2 exec (seqscan, filter, nested_loop_join, …).
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
from opt.dp import RelInfo, PlanCell, is_attr_equality, optimize_joins
from opt.rewrite import rewrite

_JOIN_PHYS = frozenset({"nested_loop_join", "hash_join", "sort_merge_join"})


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


def plan_physical(
    logical: dict,
    catalog,
    B: int = 32,
    *,
    disable_rules: set[int] | frozenset[int] | None = None,
    already_rewritten: dict | None = None,
) -> dict:
    """Rewrite then physicalize; return the physical plan dict only.

    disable_rules / already_rewritten support REPORT_M4 rule ablation without
    double-rewriting.
    """
    rewritten = (
        already_rewritten
        if already_rewritten is not None
        else rewrite(logical, disable=disable_rules)
    )
    stats_map = load_stats_map(catalog)
    phys, _meta = _phys(rewritten, stats_map, B, catalog)
    return phys


def explain_physical(
    logical: dict,
    catalog,
    B: int = 32,
    *,
    disable_rules: set[int] | frozenset[int] | None = None,
) -> tuple[dict, dict, list[str]]:
    """Same as plan_physical, plus a before-rewrite copy and indented explain lines.

    Returns (before_rewrite, physical, explain_lines).
    """
    before = copy.deepcopy(logical)
    rewritten = rewrite(logical, disable=disable_rules)
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
            table = node["table"]
            st = stats_map.get(table) or empty_defaults()
            tree = {"op": "seqscan", "table": table}
            rows, pages = st["n_tuples"], st["n_pages"]
            cost = cost_seqscan(pages)
            width = float(st.get("width", 32))
            annot[id(tree)] = {
                "est_rows": rows,
                "est_cost": cost,
                "label": f"seqscan {table}",
                "est_pages": pages,
                "width": width,
            }
            return tree, rows, pages, cost

        # --- unary ----------------------------------------------------------
        if op == "rename":
            child_p, rows, pages, cost = walk(node["child"])
            child_p = _apply_alias(child_p, node["to"])
            return child_p, rows, pages, cost

        if op == "select":
            child_p, rows, pages, cost = walk(node["child"])
            sel = selectivity(node["condition"], stats_map, rows)
            out_rows = max(1, math.ceil(rows * sel)) if rows else 0
            tree = {"op": "filter", "condition": node["condition"], "child": child_p}
            out_pages = est_pages(out_rows, 32)
            width = float(annot.get(id(child_p), {}).get("width", 32))
            annot[id(tree)] = {
                "est_rows": out_rows,
                "est_cost": 0,
                "label": f"filter ({node['condition']})",
                "est_pages": out_pages,
                "width": width,
            }
            return tree, out_rows, out_pages, cost

        if op == "project":
            child_p, rows, pages, cost = walk(node["child"])
            tree = {"op": "project", "columns": node["columns"], "child": child_p}
            proj_width = max(8.0, 8.0 * len(node["columns"]))
            out_pages = est_pages(rows, proj_width)
            # Join-output width stays the relation width (see opt.dp._result_width).
            # The slimmed proj_width is only this node's own page estimate.
            width = float(annot.get(id(child_p), {}).get("width", 32))
            annot[id(tree)] = {
                "est_rows": rows,
                "est_cost": 0,
                "label": "project",
                "est_pages": out_pages,
                "width": width,
            }
            return tree, rows, out_pages, cost

        # --- joins: Selinger DP over the whole cluster ----------------------
        if op in ("join", "cross"):
            cluster = _extract_join_cluster(node)
            if cluster is not None and len(cluster["rels"]) >= 2:
                return _physicalize_join_cluster(cluster, stats_map, B, walk, annot)
            # degenerate single-edge fallback (should be rare)
            left_p, lr, lp, lc = walk(node["left"])
            right_p, rr, rp, rc = walk(node["right"])
            cond = node.get("condition", "1 = 1")
            equality = op == "join" and "=" in cond and "!=" not in cond
            jop, jcost = choose_join(lp, rp, B, equality=equality)
            rows = (
                join_cardinality(lr, rr, cond, stats_map)
                if op == "join"
                else max(1, lr * rr)
            )
            pages = est_pages(rows, 32)
            tree = {
                "op": jop,
                "condition": cond if op == "join" else "1 = 1",
                "left": left_p,
                "right": right_p,
            }
            annot[id(tree)] = {
                "est_rows": rows,
                "est_cost": jcost,
                "label": f"{jop} ({tree['condition']})",
            }
            return tree, rows, pages, lc + rc + jcost

        # --- blocking / order ops -------------------------------------------
        if op == "group_by":
            child_p, rows, pages, cost = walk(node["child"])
            mode = "hash" if pages <= max(1, B - 1) else "sort"
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
            rows = lr + rr if op == "union_all" else max(lr, rr)
            annot[id(tree)] = {"est_rows": rows, "est_cost": 0, "label": op}
            return tree, rows, est_pages(rows, 32), lc + rc

        raise ValueError(f"unknown logical op {op}")

    phys, rows, pages, cost = walk(n)
    annot[id(phys)] = annot.get(
        id(phys), {"est_rows": rows, "est_cost": cost, "label": phys.get("op")}
    )
    return phys, annot


def _relation_id(node: dict, alias: str | None = None) -> tuple[str, str]:
    """(rel_id, base_table) for an access-path subtree."""
    op = node["op"]
    if op == "scan":
        return (alias or node["table"], node["table"])
    if op == "rename":
        return _relation_id(node["child"], alias=node["to"])
    if "child" in node:
        return _relation_id(node["child"], alias)
    raise ValueError(f"access path has no scan: {op}")


def _edge_from_condition(cond: str) -> tuple[str, str, str] | None:
    try:
        ast = parse_condition(cond.strip())
    except Exception:
        return None
    if (
        isinstance(ast, Compare)
        and ast.op == "="
        and isinstance(ast.left, Attr)
        and isinstance(ast.right, Attr)
        and ast.left.relation
        and ast.right.relation
    ):
        return (ast.left.relation, ast.right.relation, cond)
    return None


def _extract_join_cluster(node: dict) -> dict | None:
    """Flatten a join/cross nest into {rels, tables, edges}.

    Intermediate projects that wrap nested joins are peeled (column pruning
    only); projects/filters on a single base relation stay as access paths.
    """
    if node.get("op") not in ("join", "cross"):
        return None
    rels: dict[str, dict] = {}
    tables: dict[str, str] = {}
    edges: list[tuple[str, str, str]] = []

    def rec(n: dict) -> None:
        op = n["op"]
        if op == "project":
            child = n["child"]
            if child["op"] in ("join", "cross"):
                rec(child)
                return
        if op in ("join", "cross"):
            rec(n["left"])
            rec(n["right"])
            if op == "join":
                edge = _edge_from_condition(n["condition"])
                if edge is not None:
                    edges.append(edge)
            return
        rid, table = _relation_id(n)
        rels[rid] = n
        tables[rid] = table

    rec(node)
    if len(rels) < 2:
        return None
    return {"rels": rels, "tables": tables, "edges": edges}


def _physicalize_join_cluster(
    cluster: dict,
    stats_map: dict,
    B: int,
    walk,
    annot: dict,
) -> tuple[dict, int, float, float]:
    """Physicalize access paths, run left-deep DP, annotate join nodes."""
    rels_logical = cluster["rels"]
    tables = cluster["tables"]
    edges = cluster["edges"]

    relations: list[RelInfo] = []
    access_plans: dict[str, PlanCell] = {}

    for rid, logical_path in rels_logical.items():
        tree, rows, pages, cost = walk(logical_path)
        table = tables[rid]
        st = stats_map.get(table) or empty_defaults()
        alias = rid if rid != table else None
        relations.append(
            RelInfo(rid, table, alias, rows, pages, st.get("width", 32), st)
        )
        access_plans[rid] = PlanCell(
            cost=cost,
            rows=rows,
            pages=pages,
            tree=tree,
            rels=frozenset({rid}),
            width=float(st.get("width", 32)),
        )

    cell, _table = optimize_joins(relations, edges, B, stats_map, access_plans=access_plans)
    _annotate_dp_joins(cell.tree, annot, stats_map, B)
    return cell.tree, cell.rows, cell.pages, cell.cost


def _annotate_dp_joins(
    tree: dict, annot: dict, stats_map: dict, B: int
) -> tuple[int, float, float, float]:
    """Fill annot for join nodes created by DP; reuse access-path annots.

    Returns (rows, pages, cost, width). Pages of an access path are the
    ones walk() stored, not a fresh est_pages(rows, 32). Join output pages
    use the average input width, same as optimize_joins, so est_cost's
    jcost is computed from the same page counts as the DP cell.
    """
    if id(tree) in annot and tree.get("op") not in _JOIN_PHYS:
        m = annot[id(tree)]
        rows = int(m.get("est_rows") or 0)
        width = float(m.get("width") or 32.0)
        if "est_pages" in m:
            pages = float(m["est_pages"])
        else:
            pages = float(est_pages(rows, width))
        return rows, pages, float(m.get("est_cost") or 0), width

    op = tree.get("op")
    if op in _JOIN_PHYS:
        _lr, lp, _lc, lw = _annotate_dp_joins(tree["left"], annot, stats_map, B)
        rr, rp, _rc, rw = _annotate_dp_joins(tree["right"], annot, stats_map, B)
        cond = tree.get("condition", "1 = 1")
        equality = is_attr_equality(cond)
        _jop, jcost = choose_join(lp, rp, B, equality=equality)
        if cond == "1 = 1":
            rows = max(1, _lr * rr)
        else:
            rows = join_cardinality(_lr, rr, cond, stats_map)
        width = (lw + rw) / 2.0
        pages = est_pages(rows, width)
        annot[id(tree)] = {
            "est_rows": rows,
            "est_cost": jcost,
            "label": f"{op} ({cond})",
            "est_pages": pages,
            "width": width,
        }
        return rows, float(pages), jcost, width

    if "child" in tree:
        return _annotate_dp_joins(tree["child"], annot, stats_map, B)
    return 1, 1.0, 0.0, 32.0


def _apply_alias(node: dict, alias: str) -> dict:
    """Set as=alias on the seqscan under projects/filters.

    Mutates in place so explain annotations keyed by id(node) stay valid.
    """
    if node.get("op") == "seqscan":
        node["as"] = alias
        return node
    if "child" in node:
        _apply_alias(node["child"], alias)
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

    Used by the `opt dp-table` CLI. Shares cluster extraction with plan_physical
    so the table matches the join order that `opt plan` actually emits.
    """
    rewritten = rewrite(logical)
    stats_map = load_stats_map(catalog)

    # Find the topmost join/cross under unary wrappers (project/select/…).
    node = rewritten
    while node.get("op") not in ("join", "cross", None):
        if "child" in node:
            node = node["child"]
        elif node.get("op") in ("union", "union_all", "intersect", "except"):
            # set-op root: no single join cluster
            return None, {}
        else:
            break
    if not isinstance(node, dict) or node.get("op") not in ("join", "cross"):
        return None, {}

    cluster = _extract_join_cluster(node)
    if cluster is None or len(cluster["rels"]) < 2:
        return None, {}

    relations: list[RelInfo] = []
    for rid, logical_path in cluster["rels"].items():
        table = cluster["tables"][rid]
        st = stats_map.get(table) or empty_defaults()
        # Estimate access-path cardinality via a lightweight phys walk of filters.
        rows, pages = _estimate_access_path(logical_path, stats_map)
        alias = rid if rid != table else None
        relations.append(
            RelInfo(rid, table, alias, rows, pages, st.get("width", 32), st)
        )

    cell, table = optimize_joins(relations, cluster["edges"], B, stats_map)
    return cell, table


def _estimate_access_path(node: dict, stats_map: dict) -> tuple[int, float]:
    """Rows/pages for a local access path without building a physical tree."""
    op = node["op"]
    if op == "scan":
        st = stats_map.get(node["table"]) or empty_defaults()
        return st["n_tuples"], float(st["n_pages"])
    if op == "rename":
        return _estimate_access_path(node["child"], stats_map)
    if op == "select":
        rows, pages = _estimate_access_path(node["child"], stats_map)
        sel = selectivity(node["condition"], stats_map, rows)
        out = max(1, math.ceil(rows * sel)) if rows else 0
        return out, float(est_pages(out, 32))
    if op == "project":
        rows, _pages = _estimate_access_path(node["child"], stats_map)
        width = max(8.0, 8.0 * len(node.get("columns") or []))
        return rows, float(est_pages(rows, width))
    if "child" in node:
        return _estimate_access_path(node["child"], stats_map)
    return 1, 1.0
