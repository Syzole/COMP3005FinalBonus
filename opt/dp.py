"""Left-deep Selinger DP over join relations (≤6).

Selinger-style dynamic programming for join order:
  - State = a set S of base relations already joined
  - Value = cheapest PlanCell that produces S (cost, rows, pages, physical tree)
  - Transition = take best(S - {r}) as left, singleton {r} as right (left-deep only)

We only consider left-deep trees (bushy plans are out of scope for ≤6 rels
and keep the search small: O(n·2^n) subsets × O(n) choices of r).

Edges come from physicalize.build_dp_for_joins as (left_id, right_id, cond).
A subset is joined only through an edge that touches the new relation. On a
connected join graph, a subset with no such connection (for example {A,C}
when the predicates are A–B′ and B′–C) is left out of the table. A Cartesian
NLJ ("1 = 1") is used only when that subset has no connected plan at all.

Join I/O from choose_join already reads both inputs, so the right-hand scan
is never added again. A size-1 left is only that outer read, so its scan cost
is not added either. A larger left paid to build an intermediate; that cost
is added on top of the next join.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from engine.expr import parse_condition
from engine.tree import Attr, Compare
from opt.cost import choose_join, cost_seqscan
from opt.selectivity import join_cardinality, est_pages


@dataclass
class RelInfo:
    """One base table (after rename) that DP can join.

    id     — name used in join conditions / edge endpoints (alias if renamed)
    table  — underlying catalog table for seqscan
    alias  — optional AS name written onto the physical seqscan
    """

    id: str  # alias or table
    table: str
    alias: str | None
    n_tuples: int
    n_pages: int
    width: float
    stats: dict | None


@dataclass
class PlanCell:
    """Best plan found so far for one frozenset of relation ids."""

    cost: float
    rows: int
    pages: int
    tree: dict  # physical subtree rooted at this join (or seqscan)
    rels: frozenset
    width: float = 32.0


def extract_join_graph(logical: dict) -> tuple[list[RelInfo], list[tuple[str, str, str]], dict]:
    """Deprecated hook — use physicalize._extract_join_cluster / build_dp_for_joins."""
    return [], [], logical


def is_attr_equality(cond: str) -> bool:
    """True when cond is one Attr = Attr comparison.

    "1 = 1" parses as an equality of numbers, so it stays a nested loop.
    Uses the engine condition parser rather than scanning the string.
    """
    try:
        node = parse_condition(cond.strip())
    except Exception:
        return False
    return (
        isinstance(node, Compare)
        and node.op == "="
        and isinstance(node.left, Attr)
        and isinstance(node.right, Attr)
    )


def _subset_connected(ids: frozenset, edge_map: dict) -> bool:
    """True when every id is reachable from the others via edges inside ids."""
    if len(ids) <= 1:
        return True
    start = next(iter(ids))
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        for other in ids:
            if other in seen:
                continue
            if frozenset({cur, other}) in edge_map:
                seen.add(other)
                stack.append(other)
    return len(seen) == len(ids)


def _join_total(left: PlanCell, jcost: float) -> float:
    """Plan cost of left ⋈ right.

    jcost already charges a read of both inputs. The right input is a base
    relation, so right.cost is inside jcost. A size-1 left is only its scan,
    which is the M term inside jcost (handout size-2 is 110, not 120). A
    larger left paid to build an intermediate that jcost does not include
    (handout 110 + 420 = 530).
    """
    if len(left.rels) == 1:
        return jcost
    return left.cost + jcost


def _result_width(left: PlanCell, right: PlanCell) -> float:
    """Average of the two input widths.

    §5.1's relations are all 81.92 bytes/tuple, and the stated intermediate
    sizes (1000 rows → 20 pages, 2000 → 40) use that same width. Adding the
    widths would double those page counts and miss cost 530.
    """
    return (left.width + right.width) / 2.0


def optimize_joins(
    relations: list[RelInfo],
    edges: list[tuple[str, str, str]],
    B: int,
    stats_map: dict,
    access_plans: dict[str, PlanCell] | None = None,
) -> tuple[PlanCell, dict]:
    """Fill the DP table and return (best_full_plan, entire_table).

    best[S] = cheapest PlanCell whose leaves are exactly the ids in S.
    After filling sizes 1..n, best[all_ids] is the optimal left-deep plan.

    access_plans: optional pre-physicalized access paths (scan + local
    filters/projects) keyed by relation id. When present, those seed size-1
    instead of bare seqscans — used by opt plan so pushdown is preserved.

    Raises ValueError when more than six relations are passed. The spec
    limits left-deep DP to that size.
    """
    if len(relations) > 6:
        raise ValueError(
            f"join enumeration supports at most 6 relations, got {len(relations)}"
        )

    access_plans = access_plans or {}

    # undirected edge lookup: frozenset({A,B}) → "A.x = B.y"
    edge_map: dict[frozenset, str] = {}
    for a, b, cond in edges:
        edge_map[frozenset({a, b})] = cond

    best: dict[frozenset, PlanCell] = {}

    # --- size-1: access path or bare seqscan --------------------------------
    for r in relations:
        if r.id in access_plans:
            seed = access_plans[r.id]
            # Join output width follows the relation's average record width.
            seed.width = r.width
            best[frozenset({r.id})] = seed
            continue
        tree = {"op": "seqscan", "table": r.table}
        if r.alias:
            tree["as"] = r.alias
        best[frozenset({r.id})] = PlanCell(
            cost=cost_seqscan(r.n_pages),
            rows=r.n_tuples,
            pages=r.n_pages,
            tree=tree,
            rels=frozenset({r.id}),
            width=r.width,
        )

    ids = [r.id for r in relations]
    n = len(ids)
    full_ids = frozenset(ids)
    graph_connected = _subset_connected(full_ids, edge_map)

    # --- size k=2..n: try every left-deep split S = S1 ⋈ {r} ----------------
    for k in range(2, n + 1):
        for subset in itertools.combinations(ids, k):
            S = frozenset(subset)
            # {A,C} on the connected A–B′–C graph has no predicate. Leave it
            # out. A real cross product (the whole graph is disconnected)
            # still falls through to the Cartesian branch below.
            if graph_connected and not _subset_connected(S, edge_map):
                continue

            cand: PlanCell | None = None

            # try each r ∈ S as the rightmost (newest) relation
            for r in subset:
                S1 = S - {r}
                if S1 not in best or frozenset({r}) not in best:
                    continue
                left = best[S1]
                right = best[frozenset({r})]

                # need a join predicate that links r to something already in S1
                cond = None
                for a in S1:
                    key = frozenset({a, r})
                    if key in edge_map:
                        cond = edge_map[key]
                        break
                if cond is None:
                    continue

                equality = is_attr_equality(cond)
                op, jcost = choose_join(left.pages, right.pages, B, equality=equality)
                rows = join_cardinality(left.rows, right.rows, cond, stats_map)
                width = _result_width(left, right)
                pages = est_pages(rows, width)
                total = _join_total(left, jcost)
                tree = {
                    "op": op,
                    "condition": cond,
                    "left": left.tree,
                    "right": right.tree,
                }
                cell = PlanCell(total, rows, pages, tree, S, width)
                if cand is None or cell.cost < cand.cost:
                    cand = cell

            # Cartesian only when this subset itself is disconnected.
            if cand is None and not _subset_connected(S, edge_map):
                for r in subset:
                    S1 = S - {r}
                    if S1 not in best or frozenset({r}) not in best:
                        continue
                    left = best[S1]
                    right = best[frozenset({r})]
                    op, jcost = choose_join(left.pages, right.pages, B, equality=False)
                    rows = max(1, left.rows * right.rows)
                    width = _result_width(left, right)
                    pages = est_pages(rows, width)
                    tree = {
                        "op": op,
                        "condition": "1 = 1",
                        "left": left.tree,
                        "right": right.tree,
                    }
                    cell = PlanCell(_join_total(left, jcost), rows, pages, tree, S, width)
                    if cand is None or cell.cost < cand.cost:
                        cand = cell

            if cand is not None:
                best[S] = cand

    return best[full_ids], best


def dp_table_lines(best: dict[frozenset, PlanCell]) -> list[str]:
    """Pretty-print the DP table for `opt dp-table` (sorted by |S|, then names)."""
    lines = []
    for S, cell in sorted(best.items(), key=lambda kv: (len(kv[0]), sorted(kv[0]))):
        rels = ",".join(sorted(S))
        lines.append(
            f"{{{rels}}}  cost={cell.cost:.1f}  rows={cell.rows}  op={cell.tree.get('op')}"
        )
    return lines
