"""Left-deep Selinger DP over join relations (≤6).

Selinger-style dynamic programming for join order:
  - State = a set S of base relations already joined
  - Value = cheapest PlanCell that produces S (cost, rows, pages, physical tree)
  - Transition = take best(S\{r}) as left, singleton {r} as right (left-deep only)

We only consider left-deep trees (bushy plans are out of scope for ≤6 rels
and keep the search small: O(n·2^n) subsets × O(n) choices of r).

Edges come from physicalize.build_dp_for_joins as (left_id, right_id, cond).
A subset S is only joined via an edge that connects some a∈S\{r} to r; if
no such edge exists we fall back to a Cartesian NLJ with condition "1 = 1".
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

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


def extract_join_graph(logical: dict) -> tuple[list[RelInfo], list[tuple[str, str, str]], dict]:
    """Stub: real extraction lives in physicalize.build_dp_for_joins.

    Kept so callers expecting (relations, edges, leftover) have a hook;
    currently returns empty rels/edges and the plan unchanged.
    """
    rels: dict[str, RelInfo] = {}
    edges: list[tuple[str, str, str]] = []

    def add_scan(n, alias=None):
        # filled by caller with stats
        pass

    return list(rels.values()), edges, logical


def optimize_joins(
    relations: list[RelInfo],
    edges: list[tuple[str, str, str]],
    B: int,
    stats_map: dict,
) -> tuple[PlanCell, dict]:
    """Fill the DP table and return (best_full_plan, entire_table).

    best[S] = cheapest PlanCell whose leaves are exactly the ids in S.
    After filling sizes 1..n, best[all_ids] is the optimal left-deep plan.
    """
    by_id = {r.id: r for r in relations}

    # undirected edge lookup: frozenset({A,B}) → "A.x = B.y"
    edge_map: dict[frozenset, str] = {}
    for a, b, cond in edges:
        edge_map[frozenset({a, b})] = cond

    best: dict[frozenset, PlanCell] = {}

    # --- size-1: every relation is just a seqscan ---------------------------
    for r in relations:
        tree = {"op": "seqscan", "table": r.table}
        if r.alias:
            tree["as"] = r.alias
        best[frozenset({r.id})] = PlanCell(
            cost=cost_seqscan(r.n_pages),
            rows=r.n_tuples,
            pages=r.n_pages,
            tree=tree,
            rels=frozenset({r.id}),
        )

    ids = [r.id for r in relations]
    n = len(ids)

    # --- size k=2..n: try every left-deep split S = S1 ⋈ {r} ----------------
    for k in range(2, n + 1):
        for subset in itertools.combinations(ids, k):
            S = frozenset(subset)
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
                    # no connecting edge → skip; Cartesian path runs below
                    continue

                equality = "=" in cond and "!=" not in cond
                op, jcost = choose_join(left.pages, right.pages, B, equality=equality)
                rows = join_cardinality(left.rows, right.rows, cond, stats_map)
                width = 32.0
                pages = est_pages(rows, width)
                # cost of plan = cost of building left + right + this join I/O
                total = left.cost + right.cost + jcost
                tree = {
                    "op": op,
                    "condition": cond,
                    "left": left.tree,
                    "right": right.tree,
                }
                cell = PlanCell(total, rows, pages, tree, S)
                if cand is None or cell.cost < cand.cost:
                    cand = cell

            if cand is None:
                # no equality edge connected this subset — allow Cartesian
                # product via NLJ with true predicate so DP still completes
                for r in subset:
                    S1 = S - {r}
                    if S1 not in best:
                        continue
                    left = best[S1]
                    right = best[frozenset({r})]
                    op, jcost = choose_join(left.pages, right.pages, B, equality=False)
                    rows = max(1, left.rows * right.rows)
                    pages = est_pages(rows, 32.0)
                    tree = {
                        "op": op,
                        "condition": "1 = 1",
                        "left": left.tree,
                        "right": right.tree,
                    }
                    cell = PlanCell(left.cost + right.cost + jcost, rows, pages, tree, S)
                    if cand is None or cell.cost < cand.cost:
                        cand = cell

            if cand is not None:
                best[S] = cand

    full = frozenset(ids)
    return best[full], best


def dp_table_lines(best: dict[frozenset, PlanCell]) -> list[str]:
    """Pretty-print the DP table for `opt dp-table` (sorted by |S|, then names)."""
    lines = []
    for S, cell in sorted(best.items(), key=lambda kv: (len(kv[0]), sorted(kv[0]))):
        rels = ",".join(sorted(S))
        lines.append(
            f"{{{rels}}}  cost={cell.cost:.1f}  rows={cell.rows}  op={cell.tree.get('op')}"
        )
    return lines
