"""Run a physical plan and collect rows plus counters."""

from __future__ import annotations

from exec.budget import ExecError
from exec.context import Context
from exec.ops import END
from exec.plan import build_plan


def walk(op):
    yield op
    for child in op.children:
        yield from walk(child)


def totals(op) -> dict:
    out = {
        "tuples_out": op.counters()["tuples_out"],
        "comparisons": 0,
        "page_reads": 0,
        "page_writes": 0,
    }
    for node in walk(op):
        c = node.counters()
        out["comparisons"] += c["comparisons"]
        out["page_reads"] += c["page_reads"]
        out["page_writes"] += c["page_writes"]
    return out


def trace_lines(op, depth: int = 0) -> list[str]:
    c = op.counters()
    indent = "  " * depth
    label = op.trace_label()
    line = (
        f"{indent}{label:<18} tuples_out={c['tuples_out']:<6} "
        f"comparisons={c['comparisons']:<6} reads={c['page_reads']:<6} "
        f"writes={c['page_writes']}"
    )
    lines = [line]
    for child in op.children:
        lines.extend(trace_lines(child, depth + 1))
    return lines


def execute_plan(db_dir, plan, frames: int) -> dict:
    ctx = Context(db_dir, frames)
    root = None
    opened = False
    finished = False
    try:
        root = build_plan(plan, ctx)
        root.prepare()
        root.open()
        opened = True
        rows = []
        while True:
            tup = root.next()
            if tup is END:
                break
            rows.append(list(tup))
        finished = True
        return {
            "schema": root.schema,
            "rows": rows,
            "totals": totals(root),
            "root": root,
            "budget": ctx.budget,
            "ctx": ctx,
        }
    finally:
        if opened and root is not None:
            root.close()
        ctx.cleanup()
        if finished and ctx.budget.held != 0:
            raise ExecError(f"plan leaked {ctx.budget.held} frames")
