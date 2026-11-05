"""Build an operator tree from a validated physical plan dict."""

from __future__ import annotations

from exec.budget import assign_frames
from exec.ops_scan import SeqScan, Filter, Project, Limit, UnionAll
from exec.ops_sort import Sort
from exec.ops_agg import Distinct, GroupBy
from exec.ops_join import NestedLoopJoin, SortMergeJoin, HashJoin
from exec.ops_set import Union, Intersect, Except


def build_plan(ctx, plan: dict):
    alloc = assign_frames(plan, ctx.B)
    return _build(ctx, plan, alloc)


def _build(ctx, node: dict, alloc: dict):
    op = node["op"]
    frames = alloc.get(id(node))

    if op == "seqscan":
        return SeqScan(ctx, node["table"], alias=node.get("as"))
    if op == "filter":
        return Filter(_build(ctx, node["child"], alloc), node["condition"])
    if op == "project":
        return Project(_build(ctx, node["child"], alloc), node["columns"])
    if op == "limit":
        return Limit(_build(ctx, node["child"], alloc), node["n"])
    if op == "sort":
        return Sort(ctx, _build(ctx, node["child"], alloc), node["keys"], frames=frames)
    if op == "distinct":
        return Distinct(
            ctx, _build(ctx, node["child"], alloc), node["mode"], frames=frames
        )
    if op == "group_by":
        return GroupBy(
            ctx,
            _build(ctx, node["child"], alloc),
            node["mode"],
            node["keys"],
            node["aggregates"],
            frames=frames,
        )
    if op == "nested_loop_join":
        return NestedLoopJoin(
            ctx,
            _build(ctx, node["left"], alloc),
            _build(ctx, node["right"], alloc),
            node["condition"],
            frames=frames,
        )
    if op == "sort_merge_join":
        return SortMergeJoin(
            ctx,
            _build(ctx, node["left"], alloc),
            _build(ctx, node["right"], alloc),
            node["condition"],
            frames=frames,
        )
    if op == "hash_join":
        return HashJoin(
            ctx,
            _build(ctx, node["left"], alloc),
            _build(ctx, node["right"], alloc),
            node["condition"],
            frames=frames,
        )
    if op == "union_all":
        return UnionAll(
            _build(ctx, node["left"], alloc),
            _build(ctx, node["right"], alloc),
        )
    if op == "union":
        return Union(
            ctx,
            _build(ctx, node["left"], alloc),
            _build(ctx, node["right"], alloc),
            frames=frames,
        )
    if op == "intersect":
        return Intersect(
            ctx,
            _build(ctx, node["left"], alloc),
            _build(ctx, node["right"], alloc),
            frames=frames,
        )
    if op == "except":
        return Except(
            ctx,
            _build(ctx, node["left"], alloc),
            _build(ctx, node["right"], alloc),
            frames=frames,
        )
    raise RuntimeError(f"unhandled op {op}")
