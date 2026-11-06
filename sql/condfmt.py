"""Format resolved SQL conditions as plan strings."""

from __future__ import annotations

from sql import ast as A


def fmt_operand(op) -> str:
    if isinstance(op, A.LitInt):
        return str(op.value)
    if isinstance(op, A.LitStr):
        # escape quotes
        s = op.value.replace("'", "''")
        return f"'{s}'"
    if isinstance(op, A.ColRef):
        return op.resolved or (f"{op.table}.{op.name}" if op.table else op.name)
    if isinstance(op, A.Aggregate):
        # After resolve, HAVING predicates refer to the aggregate output name
        # (e.g. "n > 5"), matching M3spec §5.4.
        if op.out_name:
            return op.out_name
        if op.arg == "*":
            arg = "*"
        else:
            arg = op.arg.resolved
        return f"{op.fn}({arg})"
    return str(op)


def fmt_cond(cond) -> str:
    if isinstance(cond, A.Compare):
        # Engine condition parser uses != (not <>).
        op = "!=" if cond.op in ("!=", "<>") else cond.op
        return f"{fmt_operand(cond.left)} {op} {fmt_operand(cond.right)}"
    if isinstance(cond, A.And):
        return f"({fmt_cond(cond.left)}) and ({fmt_cond(cond.right)})"
    if isinstance(cond, A.Or):
        return f"({fmt_cond(cond.left)}) or ({fmt_cond(cond.right)})"
    if isinstance(cond, A.Not):
        return f"not ({fmt_cond(cond.cond)})"
    raise ValueError(type(cond))
