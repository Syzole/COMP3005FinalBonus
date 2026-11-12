"""Logical plan rewrite rules 1–7 (M4spec §3).

Each rule walks the tree bottom-up via `_map`, then maybe rewrites the
current node. `rewrite()` keeps applying the full rule sequence until a
pass leaves the plan unchanged (fixed point).

Condition strings in the plan are the same dialect M3 emits via
`sql.condfmt` and M2 evaluates via `engine.expr.parse_condition`. We reuse
that engine parser/AST here, cause i cant use regex and want to avoid reinventing both regex and my own code

Why this order matters:
  Rule 1 must run before 2/3 — pushdown and cross→join need single predicates.
  Rule 2 must run before 3 — single-sided filters leave only the join equality on top of the cross, which rule 3 then converts.
  Rule 6 runs after joins exist so we can insert slim projections under them.

Worked example (data/m4.json):
  before: project → select(AND of 3) → cross(rename Emp, rename Dept)
  after:  project → join(did) → project/select per side → rename → scan
"""

from __future__ import annotations

import copy

from engine.expr import parse_condition
from engine.tree import And, Attr, Compare, Not, Num, Or, Str


def rewrite(plan: dict, disable: set[int] | frozenset[int] | None = None) -> dict:
    """Apply rules 1–7 until a fixed point (plan unchanged for a full pass).

    Deep-copies so the caller's plan dict is never mutated in place.
    The pass limit is only a bug guard — if two rules ever oscillate, we
    fail loudly instead of hanging.

    disable: optional set of rule numbers (1..7) to skip — used for REPORT_M4
    ablation (“which rule mattered”).
    """
    skip = set(disable or ())
    node = copy.deepcopy(plan)
    # High ceiling: real plans should normalize in a few passes.
    # Hitting this means a rule is undoing another (should not happen).
    max_passes = 10_000
    for pass_no in range(1, max_passes + 1):
        before = copy.deepcopy(node) # make a base copy of the node before we rule them out
        if 1 not in skip:
            node = _rule1_split(node)
        if 2 not in skip:
            node = _rule2_push_select(node)
        if 3 not in skip:
            node = _rule3_cross_to_join(node)
        if 4 not in skip:
            node = _rule4_push_rename_union(node)
        if 5 not in skip:
            node = _rule5_push_below_groupby(node)
        if 6 not in skip:
            node = _rule6_push_project(node)
        if 7 not in skip:
            node = _rule7_cleanup(node)
        if node == before: #if the node is the same, then we is done and return it
            break
    else:
        raise RuntimeError(
            f"rewrite did not reach a fixed point after {max_passes} passes "
            "(likely oscillating rules)"
        )
    # Cosmetic only, and not fed back into rule 1. Merging selects inside the
    # loop would split them again and the fixed point would never arrive.
    if 7 not in skip:
        node = _rule7_finish(node)
    return node


def _map(n: dict, fn) -> dict:
    """Shallow-copy `n` and recursively transform each child with `fn`.

    Logical plans are trees of dicts:
      unary ops  → "child"
      binary ops → "left" / "right"  (join, cross, union, …)
    Bottom-up: children are rewritten first, then the caller rewrites `n`.
    """
    n = dict(n)
    if "child" in n:
        n["child"] = fn(n["child"])
    if "left" in n:
        n["left"] = fn(n["left"])
    if "right" in n:
        n["right"] = fn(n["right"])
    return n


# --- condition helpers (engine AST + M3 naming) -----------------------------
# Plan JSON stores conditions as strings. Parse once with the Bonus-1/engine
# condition parser (same as exec.compare.parse_cond), then walk the AST.


def _parse(cond: str):
    """Parse a plan condition string into an engine AST node."""
    return parse_condition(cond)


def _fmt_operand(node) -> str:
    """Format an operand for round-trip back into parse_condition.

    Mirrors sql.condfmt.fmt_operand / engine.tree.format_operand, but quotes
    string literals the way the engine lexer expects.
    """
    if isinstance(node, Attr):
        return f"{node.relation}.{node.name}" if node.relation else node.name
    if isinstance(node, Num):
        return str(node.value)
    if isinstance(node, Str):
        s = node.value.replace("'", "''")
        return f"'{s}'"
    return str(node)


def _fmt_cond(node) -> str:
    """Format a condition AST back to a plan string (M3/engine dialect)."""
    if isinstance(node, Compare):
        return f"{_fmt_operand(node.left)} {node.op} {_fmt_operand(node.right)}"
    if isinstance(node, And):
        # Parentheses match sql.condfmt style and keep precedence obvious.
        return f"({_fmt_cond(node.left)}) and ({_fmt_cond(node.right)})"
    if isinstance(node, Or):
        return f"({_fmt_cond(node.left)}) or ({_fmt_cond(node.right)})"
    if isinstance(node, Not):
        return f"not ({_fmt_cond(node.cond)})"
    return str(node)


def _flatten_and(node) -> list:
    """Flatten a nested And tree into conjunct AST nodes via recursion(rule 1)."""
    if isinstance(node, And):
        return _flatten_and(node.left) + _flatten_and(node.right)
    return [node]


def _split_and(cond: str) -> list[str]:
    """Split a condition string on AND via the engine AST (not char scanning).

    `((e.did = d.did) and (e.age > 30)) and (d.budget > 1000)`
      → [`e.did = d.did`, `e.age > 30`, `d.budget > 1000`]
    """
    parts = _flatten_and(_parse(cond))
    return [_fmt_cond(p) for p in parts]


def _walk_attrs(node, out: list[Attr]) -> None:
    """Collect every Attr under a condition AST."""
    if isinstance(node, Attr):
        out.append(node)
    elif isinstance(node, Compare):
        _walk_attrs(node.left, out)
        _walk_attrs(node.right, out)
    elif isinstance(node, (And, Or)):
        _walk_attrs(node.left, out)
        _walk_attrs(node.right, out)
    elif isinstance(node, Not):
        _walk_attrs(node.cond, out)


def _cols_in(cond: str) -> set[str]:
    """Qualified column names in a condition: `e.age`, `d.did`, …

    Uses engine Attr nodes (relation + name). Bare names (HAVING outs) are
    ignored here — see `_bare_names` / `_has_agg_ref`.
    """
    attrs: list[Attr] = []
    _walk_attrs(_parse(cond), attrs)
    return {f"{a.relation}.{a.name}" for a in attrs if a.relation}


def _bare_names(cond: str) -> set[str]:
    """Unqualified Attr names (e.g. count_star, n from COUNT(*) AS n)."""
    attrs: list[Attr] = []
    _walk_attrs(_parse(cond), attrs)
    return {a.name for a in attrs if not a.relation}


def _has_agg_ref(cond: str, group_by_node: dict) -> bool:
    """True if the condition references an aggregate output (HAVING-like).

    Reuses M3's group_by plan shape: aggregates are [fn, arg, out_name].
    Also recognizes default names from sql.resolve._agg_name
    (count_star, sum_Emp_salary, …) as a fallback.
    """
    bares = _bare_names(cond)
    if not bares:
        return False

    # Out-names recorded on the group_by node by M3 translate.
    agg_outs = {
        trip[2]
        for trip in (group_by_node.get("aggregates") or [])
        if isinstance(trip, (list, tuple)) and len(trip) >= 3
    }
    if bares & agg_outs:
        return True

    # Fallback: M3 default naming (sql.resolve._agg_name).
    prefixes = ("count_", "sum_", "avg_", "min_", "max_")
    for name in bares:
        lower = name.lower()
        if lower == "count_star" or any(lower.startswith(p) for p in prefixes):
            return True
    return False


def _is_equality(cond: str) -> bool:
    """True iff the condition is a single Compare with op '=' (rule 3)."""
    node = _parse(cond)
    return isinstance(node, Compare) and node.op == "="


def _side_cols(node: dict) -> set[str]:
    """Alias / table qualifiers available under `node`.

    After M3, scans are usually wrapped in rename, so we return the alias
    (`e`, `d`) rather than the physical table name. That matches how
    conditions are written (`e.did = d.did`).
    """
    op = node.get("op")
    if op == "scan":
        return {node["table"]}
    if op == "rename":
        return {node["to"]}
    if op == "seqscan":
        # Physical plans may already use seqscan with optional `as`.
        return {node.get("as") or node["table"]}
    s: set[str] = set()
    for k in ("child", "left", "right"):
        if k in node:
            s |= _side_cols(node[k])
    return s


# --- rule 1: split conjunctions ---------------------------------------------
# select[p AND q AND r]  →  select[p] → select[q] → select[r] → child
# Always safe. Makes rule 2 able to push each predicate independently.


def _rule1_split(n: dict) -> dict:
    n = _map(n, _rule1_split)
    if n.get("op") == "select":
        parts = _split_and(n["condition"])
        if len(parts) > 1:
            # Stack selects bottom-up so the first conjunct ends up closest
            # to the child (order among ANDs does not change semantics).
            cur = n["child"]
            for p in reversed(parts):
                cur = {"op": "select", "condition": p, "child": cur}
            return cur
    return n


# --- rule 2: push select under join/cross -----------------------------------
# select[p] over join/cross → push p onto the one side whose columns p uses.
# Precondition: p references columns from exactly one side.
# (Join predicates that touch both sides stay above — rule 3 may consume them.)


def _rule2_push_select(n: dict) -> dict:
    n = _map(n, _rule2_push_select)
    if n.get("op") != "select":
        return n
    child = n["child"]
    if child.get("op") not in ("join", "cross"):
        return n

    cols = _cols_in(n["condition"])
    left_q = _side_cols(child["left"])
    right_q = _side_cols(child["right"])

    # Every referenced qualifier must live on one side only.
    left_hit = all(c.split(".")[0] in left_q for c in cols) if cols else False
    right_hit = all(c.split(".")[0] in right_q for c in cols) if cols else False

    if left_hit and not right_hit:
        child = dict(child)
        child["left"] = {"op": "select", "condition": n["condition"], "child": child["left"]}
        return child  # drop the outer select; it now lives on the left
    if right_hit and not left_hit:
        child = dict(child)
        child["right"] = {"op": "select", "condition": n["condition"], "child": child["right"]}
        return child
    return n  # both sides / neither → leave in place


# --- rule 3: cross + equality select → join ---------------------------------
# select[R.a = S.b] over cross(R, S)  →  join[R.a = S.b](R, S)
# This is what makes a comma join as good as JOIN ON after pushdown.
# Only a single equality Compare; inequality / AND / OR stay as filter.


def _rule3_cross_to_join(n: dict) -> dict:
    n = _map(n, _rule3_cross_to_join)
    if n.get("op") != "select":
        return n
    child = n["child"]
    if child.get("op") != "cross":
        return n

    cond = n["condition"]
    if not _is_equality(cond):
        return n

    cols = _cols_in(cond)
    left_q = _side_cols(child["left"])
    right_q = _side_cols(child["right"])
    if not cols:
        return n

    touches_l = any(c.split(".")[0] in left_q for c in cols)
    touches_r = any(c.split(".")[0] in right_q for c in cols)
    if touches_l and touches_r:
        return {
            "op": "join",
            "condition": cond,
            "left": child["left"],
            "right": child["right"],
        }
    return n


# --- rule 4: push select under rename / union -------------------------------
# select over rename → rename over select (filter closer to the scan).
# select over union → push into BOTH branches (same predicate on each).
# Conditions already use the rename alias from M3, so we keep the text as-is.


def _rule4_push_rename_union(n: dict) -> dict:
    n = _map(n, _rule4_push_rename_union)
    if n.get("op") != "select":
        return n
    child = n["child"]

    if child.get("op") == "rename":
        return {
            "op": "rename",
            "to": child["to"],
            "child": {"op": "select", "condition": n["condition"], "child": child["child"]},
        }

    if child.get("op") in ("union", "union_all"):
        return {
            "op": child["op"],
            "left": {"op": "select", "condition": n["condition"], "child": child["left"]},
            "right": {"op": "select", "condition": n["condition"], "child": child["right"]},
        }

    return n


# --- rule 5: push select under group_by -------------------------------------
# Only if the predicate references grouping columns alone (WHERE-like).
# Predicates on aggregates (HAVING) must stay above the group_by.


def _rule5_push_below_groupby(n: dict) -> dict:
    n = _map(n, _rule5_push_below_groupby)
    if n.get("op") != "select":
        return n
    child = n["child"]
    if child.get("op") != "group_by":
        return n

    keys = set(child.get("keys") or [])
    cols = _cols_in(n["condition"])
    key_bares = {k.split(".")[-1] for k in keys}

    # All referenced cols must be grouping keys (qualified or bare match).
    if cols and all(c in keys or c.split(".")[-1] in key_bares for c in cols):
        # HAVING on aggregates must stay above — use M3 aggregate out_names.
        if not _has_agg_ref(n["condition"], child):
            return {
                "op": "group_by",
                "keys": child["keys"],
                "aggregates": child["aggregates"],
                "child": {
                    "op": "select",
                    "condition": n["condition"],
                    "child": child["child"],
                },
            }
    return n


# --- rule 6: push projections under join ------------------------------------
# project[needed] over join → also project each side down to
#   (columns ancestors still need) ∪ (columns the join predicate needs).
# Never drop a column a predicate above still references.
# Column lists are sorted for stable JSON diffs.


def _rule6_push_project(n: dict) -> dict:
    n = _map(n, _rule6_push_project)
    if n.get("op") != "project":
        return n
    child = n["child"]
    needed = set(n.get("columns") or [])

    if child.get("op") == "join":
        # Join keys are required on each side even if not in the final SELECT.
        needed |= _cols_in(child.get("condition", ""))
        left_needed = [c for c in needed if c.split(".")[0] in _side_cols(child["left"])]
        right_needed = [c for c in needed if c.split(".")[0] in _side_cols(child["right"])]

        left = child["left"]
        right = child["right"]
        # Avoid stacking identical projects forever (fixed-point safety).
        if left_needed and left.get("op") != "project":
            left = {"op": "project", "columns": sorted(set(left_needed)), "child": left}
        if right_needed and right.get("op") != "project":
            right = {"op": "project", "columns": sorted(set(right_needed)), "child": right}

        return {
            "op": "project",
            "columns": n["columns"],
            "child": {
                "op": "join",
                "condition": child["condition"],
                "left": left,
                "right": right,
            },
        }
    return n


# --- rule 7: cosmetic cleanup -----------------------------------------------
# Inside the fixed-point loop: drop project→project with the same column list.
# After rules 1–6 settle, _rule7_finish collapses adjacent selects and drops a
# project whose column set matches its child project (order does not matter).
# Select collapse stays outside the loop so rule 1 does not split the AND again.


def _rule7_cleanup(n: dict) -> dict:
    n = _map(n, _rule7_cleanup)
    if n.get("op") == "project":
        child = n["child"]
        if child.get("op") == "project" and child.get("columns") == n.get("columns"):
            return child  # drop the outer duplicate
    return n


def _collapse_adjacent_selects(n: dict) -> dict:
    """select[p] over select[q] → select[p AND q], bottom-up so a stack collapses."""
    n = _map(n, _collapse_adjacent_selects)
    if n.get("op") != "select":
        return n
    child = n.get("child")
    if not isinstance(child, dict) or child.get("op") != "select":
        return n
    merged = _fmt_cond(And(_parse(n["condition"]), _parse(child["condition"])))
    return {"op": "select", "condition": merged, "child": child["child"]}


def _drop_same_column_project(n: dict) -> dict:
    """Drop a project that keeps exactly the columns its child project produces."""
    n = _map(n, _drop_same_column_project)
    if n.get("op") != "project":
        return n
    child = n.get("child")
    if not isinstance(child, dict) or child.get("op") != "project":
        return n
    if set(n.get("columns") or []) == set(child.get("columns") or []):
        return child
    return n


def _rule7_finish(n: dict) -> dict:
    """One cosmetic pass after rules 1–6 have stopped changing the tree."""
    n = _collapse_adjacent_selects(n)
    n = _drop_same_column_project(n)
    return n
