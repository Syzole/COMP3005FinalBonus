"""High-level SQL → logical plan pipeline."""

from __future__ import annotations

from sql.parser import parse, parse_query
from sql.resolve import resolve_statements, resolve_query
from sql.translate import translate
from sql import ast as A
from sql.errors import SqlError


def compile_sql(source: str, catalog) -> dict:
    """Parse + resolve + translate the first query statement."""
    stmts = parse(source)
    if not stmts:
        raise SqlError("syntax", "empty input")
    # Prefer first query; CREATE/INSERT are not plans
    for s in stmts:
        if isinstance(s, (A.SelectBlock, A.SetOp)):
            q = resolve_query(s, catalog)  # bind names / types against catalog
            return translate(q)  # AST → logical plan dict
    raise SqlError("syntax", "no query statement found")


def compile_file(path: str, catalog) -> dict:
    # Convenience wrapper: read a .sql file then compile
    with open(path, encoding="utf-8-sig") as f:
        return compile_sql(f.read(), catalog)


def check_sql(source: str, catalog) -> None:
    """Raise SqlError on first problem; otherwise return None."""
    compile_sql(source, catalog)  # discard plan; used for validation only


def ast_explain(source: str) -> list[str]:
    """Parse-only indented outline (no catalog)."""
    stmts = parse(source)
    lines = []
    for s in stmts:
        lines.extend(_explain_ast(s, 0))
    return lines


def _explain_ast(node, depth: int) -> list[str]:
    # Pretty-print one AST node as indented outline lines
    pad = "  " * depth
    if isinstance(node, A.SelectBlock):
        lines = [f"{pad}SELECT{' DISTINCT' if node.distinct else ''}"]
        lines.append(f"{pad}  FROM ...")
        if node.where:
            lines.append(f"{pad}  WHERE ...")
        if node.group_by:
            lines.append(f"{pad}  GROUP BY ...")
        if node.having:
            lines.append(f"{pad}  HAVING ...")
        if node.order_by:
            lines.append(f"{pad}  ORDER BY ...")
        if node.limit is not None:
            lines.append(f"{pad}  LIMIT {node.limit}")
        return lines
    if isinstance(node, A.SetOp):
        # UNION / INTERSECT / EXCEPT — recurse into both sides
        lines = [f"{pad}{node.op.upper()}"]
        lines.extend(_explain_ast(node.left, depth + 1))
        lines.extend(_explain_ast(node.right, depth + 1))
        return lines
    if isinstance(node, A.CreateTable):
        return [f"{pad}CREATE TABLE {node.name}"]
    if isinstance(node, A.Insert):
        return [f"{pad}INSERT INTO {node.table}"]
    return [f"{pad}{type(node).__name__}"]  # fallback for unknown node kinds
