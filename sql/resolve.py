"""Name resolution, type checking, aggregate rules.

Walks the parsed SQL AST and:
  - binds column/table names to catalog (or subquery) schemas
  - fills ColRef.resolved / ColRef.typ and SelectBlock.output_schema
  - enforces WHERE/HAVING/GROUP BY aggregate placement rules
  - type-checks comparisons and set-op arity/types

Downstream (translate) assumes these fields are already filled.
"""

from __future__ import annotations

from sql import ast as A
from sql.errors import SqlError
from storage.catalog import Catalog


class ScopeEntry:
    """One FROM item (base table or subquery) visible for name lookup."""

    def __init__(self, key: str, columns: list, source: str):
        """columns: [[name, type, ...], ...] bare names; source for messages."""
        self.key = key  # alias if present, else table name
        self.columns = columns  # bare schema
        # bare column name -> full schema row (for quick lookup)
        self.colmap = {c[0].split(".")[-1]: c for c in columns}
        self.source = source


def resolve_statements(stmts: list, catalog: Catalog) -> list:
    """Resolve every statement; CREATE TABLE is passed through unchanged."""
    out = []
    for s in stmts:
        if isinstance(s, A.CreateTable):
            out.append(s)  # DDL: nothing to bind
        elif isinstance(s, A.Insert):
            _resolve_insert(s, catalog)
            out.append(s)
        else:
            out.append(resolve_query(s, catalog))  # SELECT / set-op
    return out


def resolve_query(q, catalog: Catalog):
    """Resolve a SelectBlock or SetOp (UNION / INTERSECT / EXCEPT)."""
    if isinstance(q, A.SetOp):
        # Resolve both sides independently, then check compatibility.
        left = resolve_query(q.left, catalog)
        right = resolve_query(q.right, catalog)
        _check_set_types(left, right, q.op)
        q.left = left
        q.right = right
        # Output schema follows the left operand (SQL convention).
        q.output_schema = _output_schema(left)
        return q
    if isinstance(q, A.SelectBlock):
        return _resolve_select(q, catalog)
    raise SqlError("syntax", f"unexpected query node {type(q)}")


def _output_schema(q) -> list:
    """Schema list [[name, type, ...], ...] from a resolved query node."""
    if isinstance(q, A.SelectBlock):
        return q.output_schema
    if isinstance(q, A.SetOp):
        return q.output_schema
    return []


def _resolve_insert(ins: A.Insert, catalog: Catalog):
    """Check INSERT target exists and value arity matches table schema."""
    info = catalog.get_table(ins.table)
    if info is None:
        raise SqlError("name", f"unknown table {ins.table}", ins.line, ins.column)
    # Only arity checked here; value types are not fully validated.
    if len(ins.values) != len(info["schema"]):
        raise SqlError(
            "schema",
            f"INSERT arity {len(ins.values)} != table arity {len(info['schema'])}",
            ins.line,
            ins.column,
        )


def _build_scope(from_clause, catalog: Catalog) -> list[ScopeEntry]:
    """Build the FROM scope: one ScopeEntry per table/subquery (alias as key)."""
    entries: list[ScopeEntry] = []

    def add_base(tr: A.TableRef):
        # Look up base table in catalog and register under alias (or name).
        info = catalog.get_table(tr.name)
        if info is None:
            raise SqlError("name", f"unknown table {tr.name}", tr.line, tr.column)
        key = tr.alias if tr.alias else tr.name
        for e in entries:
            if e.key == key:
                raise SqlError(
                    "name", f"duplicate alias or table name {key}", tr.line, tr.column
                )
        # Catalog stores qualified names; strip to bare for colmap lookup.
        cols = [[c[0].split(".")[-1]] + list(c[1:]) for c in info["schema"]]
        entries.append(ScopeEntry(key, cols, tr.name))

    def add_sub(tr: A.TableRef):
        # Subqueries in FROM must be aliased so columns are addressable.
        if not tr.alias:
            raise SqlError("syntax", "subquery in FROM requires AS alias", tr.line, tr.column)
        sub = resolve_query(tr.subquery, catalog)  # resolve nested query first
        tr.subquery = sub
        key = tr.alias
        for e in entries:
            if e.key == key:
                raise SqlError("name", f"duplicate alias {key}", tr.line, tr.column)
        # Subquery output: use bare names from last segment of each column.
        cols = []
        for c in _output_schema(sub):
            bare = c[0].split(".")[-1]
            cols.append([bare] + list(c[1:]))
        entries.append(ScopeEntry(key, cols, key))

    def add_ref(tr: A.TableRef):
        if tr.kind == "base":
            add_base(tr)
        else:
            add_sub(tr)

    # Comma-FROM and JOIN ON both just accumulate tables into scope.
    if isinstance(from_clause, A.FromComma):
        for tr in from_clause.tables:
            add_ref(tr)
    elif isinstance(from_clause, A.JoinOn):
        add_ref(from_clause.left)
        add_ref(from_clause.right)
        for tr, _ in from_clause.more:
            add_ref(tr)
    return entries


def _resolve_col(col: A.ColRef, scope: list[ScopeEntry]) -> None:
    """Bind a ColRef to scope: set .resolved (alias.col) and .typ from schema."""
    # Qualified: t.c — look up table/alias then column.
    if col.table is not None:
        entry = next((e for e in scope if e.key == col.table), None)
        if entry is None:
            raise SqlError(
                "name", f"unknown table or alias {col.table}", col.line, col.column
            )
        if col.name not in entry.colmap:
            raise SqlError(
                "name",
                f"unknown column {col.name} in {col.table}",
                col.line,
                col.column,
            )
        spec = entry.colmap[col.name]
        col.resolved = f"{col.table}.{col.name}"
        col.typ = list(spec)
        return

    # Unqualified: must appear in exactly one scope entry.
    matches = []
    for e in scope:
        if col.name in e.colmap:
            matches.append(e)
    if len(matches) == 0:
        raise SqlError("name", f"unknown column {col.name}", col.line, col.column)
    if len(matches) > 1:
        cands = ", ".join(e.key for e in matches)
        raise SqlError(
            "ambiguity",
            f"column {col.name} is ambiguous between {cands}",
            col.line,
            col.column,
        )
    e = matches[0]
    spec = e.colmap[col.name]
    col.resolved = f"{e.key}.{col.name}"
    col.typ = list(spec)
    col.table = e.key  # fill in so later phases see a qualified ref


def _walk_cond(cond, scope, *, allow_agg=False, ban_agg=False, aggs_out=None):
    """Recursively resolve operands in a boolean condition tree."""
    if cond is None:
        return
    # Recurse through boolean connectives; type-check at Compare leaves.
    if isinstance(cond, A.And):
        _walk_cond(cond.left, scope, allow_agg=allow_agg, ban_agg=ban_agg, aggs_out=aggs_out)
        _walk_cond(cond.right, scope, allow_agg=allow_agg, ban_agg=ban_agg, aggs_out=aggs_out)
        return
    if isinstance(cond, A.Or):
        _walk_cond(cond.left, scope, allow_agg=allow_agg, ban_agg=ban_agg, aggs_out=aggs_out)
        _walk_cond(cond.right, scope, allow_agg=allow_agg, ban_agg=ban_agg, aggs_out=aggs_out)
        return
    if isinstance(cond, A.Not):
        _walk_cond(cond.cond, scope, allow_agg=allow_agg, ban_agg=ban_agg, aggs_out=aggs_out)
        return
    if isinstance(cond, A.Compare):
        _resolve_operand(cond.left, scope, allow_agg=allow_agg, ban_agg=ban_agg, aggs_out=aggs_out)
        _resolve_operand(cond.right, scope, allow_agg=allow_agg, ban_agg=ban_agg, aggs_out=aggs_out)
        _typecheck_compare(cond)
        return
    raise SqlError("syntax", f"bad condition {type(cond)}")


def _resolve_operand(op, scope, *, allow_agg=False, ban_agg=False, aggs_out=None):
    """Resolve one side of a comparison; optionally collect allowed aggregates."""
    if isinstance(op, A.ColRef):
        _resolve_col(op, scope)
        return
    if isinstance(op, (A.LitInt, A.LitStr)):
        return  # literals need no binding
    if isinstance(op, A.Aggregate):
        # WHERE bans aggs; HAVING/SELECT allow them via allow_agg.
        if ban_agg:
            raise SqlError(
                "aggregate",
                "aggregate not allowed in WHERE",
                op.line,
                op.column,
            )
        if not allow_agg:
            raise SqlError("aggregate", "aggregate not allowed here", op.line, op.column)
        _resolve_agg(op, scope)
        if aggs_out is not None:
            aggs_out.append(op)
        return
    raise SqlError("syntax", f"bad operand {type(op)}")


def _type_of_operand(op) -> str:
    """SQL type string for a resolved operand (used in comparisons / schema)."""
    if isinstance(op, A.LitInt):
        return "INT"
    if isinstance(op, A.LitStr):
        return "VARCHAR"
    if isinstance(op, A.ColRef):
        return op.typ[1]  # schema row is [name, type, ...]
    if isinstance(op, A.Aggregate):
        if op.fn in ("SUM", "COUNT"):
            return "INT"
        if op.fn == "AVG":
            return "FLOAT"
        # MIN/MAX — same type as the argument column
        if op.arg == "*":
            return "INT"
        return op.arg.typ[1]
    return "INT"


def _typecheck_compare(cmp: A.Compare):
    """Require both sides of a comparison to be the same (numeric) type family."""
    lt = _type_of_operand(cmp.left)
    rt = _type_of_operand(cmp.right)
    # FLOAT (AVG) vs INT is allowed; both count as numeric.
    def norm(t):
        return "INT" if t in ("INT", "FLOAT") else t
    if norm(lt) != norm(rt):
        raise SqlError(
            "type",
            f"type mismatch in comparison ({lt} vs {rt})",
            cmp.line,
            cmp.column,
        )


def _resolve_agg(agg: A.Aggregate, scope: list[ScopeEntry]):
    """Resolve aggregate arg column; SUM/AVG need INT; only COUNT(*) allowed."""
    if agg.arg != "*":
        _resolve_col(agg.arg, scope)
        if agg.fn in ("SUM", "AVG") and agg.arg.typ[1] != "INT":
            raise SqlError(
                "type",
                f"{agg.fn} requires INT column",
                agg.line,
                agg.column,
            )
    elif agg.fn not in ("COUNT",):
        raise SqlError("syntax", f"{agg.fn}(*) not allowed", agg.line, agg.column)
    agg.out_name = _agg_name(agg)


def _agg_name(agg: A.Aggregate) -> str:
    """Default output column name for an aggregate (unless AS alias given)."""
    if agg.alias:
        return agg.alias
    fn = agg.fn.lower()
    if agg.arg == "*":
        return "count_star" if fn == "count" else f"{fn}_star"
    col = agg.arg.resolved.replace(".", "_")
    return f"{fn}_{col}"  # e.g. sum_orders_qty


def _resolve_select(sel: A.SelectBlock, catalog: Catalog) -> A.SelectBlock:
    """Full SELECT resolution: FROM scope, clauses, SELECT list, output schema."""
    scope = _build_scope(sel.from_clause, catalog)

    # JOIN ON — same rules as WHERE (no aggregates).
    if isinstance(sel.from_clause, A.JoinOn):
        _walk_cond(sel.from_clause.condition, scope, ban_agg=True)
        for _tr, c in sel.from_clause.more:
            _walk_cond(c, scope, ban_agg=True)

    # WHERE — no aggregates
    _walk_cond(sel.where, scope, ban_agg=True)

    # GROUP BY — bind each grouping column into scope
    for g in sel.group_by:
        _resolve_col(g, scope)

    # Aggregating if GROUP BY present or any SELECT item is an aggregate.
    aggregating = bool(sel.group_by) or any(
        isinstance(it.expr, A.Aggregate) for it in sel.items if it.expr != "*"
    )

    # SELECT list -> output_schema (and star expansion if SELECT *)
    if len(sel.items) == 1 and sel.items[0].expr == "*":
        # Expand * to every column from every FROM entry, in order.
        expanded = []
        out_schema = []
        for e in scope:
            for c in e.columns:
                bare = c[0]
                q = f"{e.key}.{bare}"
                expanded.append(q)
                out_schema.append([q] + list(c[1:]))
        sel.star_expanded = expanded
        sel.output_schema = out_schema
    else:
        out_schema = []
        group_keys = {g.resolved for g in sel.group_by}
        for it in sel.items:
            if isinstance(it.expr, A.Aggregate):
                if not aggregating:
                    aggregating = True
                _resolve_agg(it.expr, scope)
                name = it.alias or it.expr.out_name
                it.expr.out_name = name
                typ = _type_of_operand(it.expr)
                if typ == "FLOAT":
                    out_schema.append([name, "FLOAT"])
                elif typ == "VARCHAR" and isinstance(it.expr.arg, A.ColRef):
                    # Preserve VARCHAR length from the underlying column.
                    out_schema.append([name] + list(it.expr.arg.typ[1:]))
                else:
                    out_schema.append([name, "INT"])
            elif isinstance(it.expr, A.ColRef):
                _resolve_col(it.expr, scope)
                # Non-aggregated columns must be grouping keys when aggregating.
                if aggregating and it.expr.resolved not in group_keys:
                    raise SqlError(
                        "aggregate",
                        f"column {it.expr.resolved} must appear in GROUP BY or aggregate",
                        it.expr.line,
                        it.expr.column,
                    )
                name = it.alias or it.expr.resolved
                out_schema.append([name] + list(it.expr.typ[1:]))
            else:
                raise SqlError("syntax", "bad select item")
        sel.output_schema = out_schema

    # HAVING — only when aggregating; bare cols must be GROUP BY keys.
    if sel.having is not None:
        if not aggregating:
            raise SqlError("aggregate", "HAVING without aggregation", sel.line, sel.column)
        # Reuse select-list aggregate output names when fn+arg match.
        known_aggs = []
        for it in sel.items:
            if isinstance(it.expr, A.Aggregate):
                known_aggs.append(it.expr)
        _walk_cond_having(
            sel.having, scope, {g.resolved for g in sel.group_by}, known_aggs
        )

    # ORDER BY — prefer SELECT aliases, then FROM scope, then output bare names.
    alias_map = {}
    for i, it in enumerate(sel.items):
        if it.expr == "*":
            continue
        if it.alias:
            alias_map[it.alias] = sel.output_schema[i][0]
        if isinstance(it.expr, A.ColRef) and it.expr.resolved:
            # also allow ordering by select output name
            pass
    for sk in sel.order_by:
        if isinstance(sk.expr, A.ColRef):
            if sk.expr.table is None and sk.expr.name in alias_map:
                sk.resolved = alias_map[sk.expr.name]  # ORDER BY select-alias
            else:
                try:
                    _resolve_col(sk.expr, scope)
                    sk.resolved = sk.expr.resolved
                except SqlError:
                    # Fall back: match against SELECT output column names.
                    found = [c[0] for c in sel.output_schema if c[0].split(".")[-1] == sk.expr.name or c[0] == sk.expr.name]
                    if len(found) == 1:
                        sk.resolved = found[0]
                    else:
                        raise

    return sel


def _agg_key(agg: A.Aggregate):
    """Identity key so HAVING can reuse a SELECT-list aggregate's out_name."""
    if agg.arg == "*":
        return (agg.fn.upper(), "*")
    return (agg.fn.upper(), agg.arg.resolved if agg.arg.resolved else agg.arg.name)


def _walk_cond_having(cond, scope, group_keys: set, known_aggs: list):
    """Resolve HAVING: aggregates ok; bare columns must be in GROUP BY."""
    if isinstance(cond, (A.And, A.Or)):
        _walk_cond_having(cond.left, scope, group_keys, known_aggs)
        _walk_cond_having(cond.right, scope, group_keys, known_aggs)
        return
    if isinstance(cond, A.Not):
        _walk_cond_having(cond.cond, scope, group_keys, known_aggs)
        return
    if isinstance(cond, A.Compare):
        for side in (cond.left, cond.right):
            if isinstance(side, A.Aggregate):
                _resolve_agg(side, scope)
                # Align out_name with a matching SELECT aggregate if present.
                for known in known_aggs:
                    if _agg_key(known) == _agg_key(side):
                        side.out_name = known.out_name
                        break
            elif isinstance(side, A.ColRef):
                _resolve_col(side, scope)
                # Bare HAVING cols must be grouping keys (SQL rule).
                if side.resolved not in group_keys:
                    raise SqlError(
                        "aggregate",
                        f"HAVING column {side.resolved} must be grouping column or aggregate",
                        side.line,
                        side.column,
                    )
            elif isinstance(side, (A.LitInt, A.LitStr)):
                pass
            else:
                raise SqlError("syntax", "bad HAVING operand")
        _typecheck_compare(cond)
        return
    raise SqlError("syntax", "bad HAVING condition")


def _check_set_types(left, right, op: str):
    """UNION/INTERSECT/EXCEPT require matching arity and per-column types."""
    ls = _output_schema(left)
    rs = _output_schema(right)
    if len(ls) != len(rs):
        raise SqlError(
            "schema",
            f"{op} arity mismatch: {len(ls)} vs {len(rs)}",
        )
    for i, (a, b) in enumerate(zip(ls, rs)):
        if a[1] != b[1]:
            raise SqlError(
                "schema",
                f"{op} type mismatch at column {i}: {a[1]} vs {b[1]}",
            )
