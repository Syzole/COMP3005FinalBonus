"""Assign frames to operators in a physical plan tree."""

#whole idea in m2 is we may at most hold B frames in memory at any time
# m2 only chooses the policy for splitting up B, uses FrameBudget in context.py to enforce

#three classes of ops:
#pipeline - stream tuples; needs about 1 frame
#blocking - must consume all input before producing
#joins - have the full B while they run (blocking children run first and release)

from __future__ import annotations

PIPELINE = {
    "seqscan", "filter", "project", "union_all", "limit",
}
BLOCKING = {
    "sort", "distinct", "group_by", "intersect", "except", "union",
}
JOINS = {
    "nested_loop_join", "sort_merge_join", "hash_join",
}


def assign_frames(node: dict, B: int) -> dict[int, int]:
    """Return map id(node_dict) -> frames. Mutates nothing; used at build time.

    Plan dict is not hashable, so we use id(node_dict) to identify nodes, so build.py is faster

    Rule: pipelined get 1; remaining frames split equally among blocking ops.
    Joins that own B take the plan B (stored on the join node).
    """
    blocking_nodes = []
    join_nodes = []

    def walk(n):
        op = n["op"]
        if op in BLOCKING:
            blocking_nodes.append(n)
        if op in JOINS:
            join_nodes.append(n)
        if "child" in n:
            walk(n["child"])
        if "left" in n:
            walk(n["left"])
        if "right" in n:
            walk(n["right"])

    walk(node)
    # Reserve 1 per pipeline leaf path roughly — simpler: give blocking equal share of B
    # Joins get full B when they run (blocking children run first and release).
    share = max(3, B // max(1, len(blocking_nodes))) if blocking_nodes else B
    alloc = {}
    for n in blocking_nodes:
        alloc[id(n)] = min(B, share)
    for n in join_nodes:
        alloc[id(n)] = B
    return alloc
