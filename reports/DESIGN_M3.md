# Design M3 design notes — SQL Front End

## Structure

1. **Lexer** (`sql/lexer.py`) — hand-written; keywords uppercased; ids preserved.
2. **Parser** (`sql/parser.py`) — recursive descent per `GRAMMAR_SQL.md`.
3. **Resolve** (`sql/resolve.py`) — scope, qualification, types, aggregates.
4. **Translate** (`sql/translate.py`) — canonical logical plan, **no** pushdown.

Errors: `error: <category>: <message> at line L, column C` (`lexical|syntax|name|ambiguity|type|aggregate|schema`). First error only.

## Name resolution algorithm

1. Build FROM scope: one entry per table_ref, keyed by alias if present else table name. Subquery requires `AS alias` and contributes its output schema (bare names) under that alias. Duplicate keys → `name` error (`FROM Emp, Emp`).
2. Qualified `t.c`: `t` must be a scope key; `c` must exist in that entry.
3. Unqualified `c`: exactly one matching entry; else unknown (message lists candidates).
4. `SELECT *`: expand every column of every scope entry, FROM order then schema order, fully qualified (`e.did`, `d.did` both OK).
5. `ORDER BY`: try select-list aliases first, then FROM scope (columns not in select list allowed).
6. After resolve, every column in the plan is fully qualified (or an aggregate/alias output name).

## Aggregation

Aggregating if and only if `GROUP BY` present or any aggregate in the select list. Select/HAVING may only use grouping columns and aggregates. Aggregates in WHERE → always `aggregate` error. `SELECT COUNT(*) FROM T` (no GROUP BY) → `group_by` with empty keys (one output row, including empty input — executed later by M2).

### Aggregate output names

- Explicit `AS alias` takes precedence over implicit (`COUNT(*) AS n` → `n`).
- Else `count_star` for `COUNT(*)`, else `{fn}_{table}_{col}` with dots → underscores (`avg_Emp_salary`).
- HAVING predicates that repeat a select-list aggregate reuse that output name (`HAVING COUNT(*) > 5` → condition `n > 5` when select has `COUNT(*) AS n`).

## Bag semantics — where duplicates are removed

| Construct | Dedup? | Plan node |
|-----------|--------|-----------|
| scan / project | no | `scan` / `project` |
| `SELECT DISTINCT` | yes | `distinct` **above** project (never auto on project) |
| `UNION ALL` | no | `union_all` |
| `UNION` | yes | `union` |
| `INTERSECT` / `EXCEPT` | yes (set) | same |
| `GROUP BY` | by construction | `group_by` only — **no** extra `distinct` |

This is the main difference from Bonus 1 set-projection and how duplicates are handled.

## Translation order

FROM (scans + renames; comma→`cross`, JOIN ON→`join`) → WHERE `select` → `group_by` → HAVING `select` → `project` → optional `distinct` → `sort` → `limit` → setops are left-associative.

Predicates are **not** pushed. Comma vs JOIN ON differ only in whether the equality sits in `select` above `cross` or on `join`.

## Readings and sources I used

1. **Ramakrishnan & Gehrke ch. 4–5** — SQL bags vs algebra sets; motivated keeping project bag-preserving.
2. **Silberschatz ch. 3 & 6** — name resolution / binding and the WHERE vs HAVING timing that drives our canonical stack.
