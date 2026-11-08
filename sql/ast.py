"""SQL AST nodes (pre-resolution)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CreateTable:
    name: str
    columns: list  # [[name, 'INT'] | [name, 'VARCHAR', n]]
    line: int = 1
    column: int = 1


@dataclass
class Insert:
    table: str
    values: list  # literals
    line: int = 1
    column: int = 1


@dataclass
class ColRef:
    name: str
    table: str | None = None  # None = unqualified
    line: int = 1
    column: int = 1
    # filled by resolve:
    resolved: str | None = None  # "t.c"
    typ: list | None = None


@dataclass
class LitInt:
    value: int
    line: int = 1
    column: int = 1


@dataclass
class LitStr:
    value: str
    line: int = 1
    column: int = 1


@dataclass
class Aggregate:
    fn: str  # COUNT|SUM|AVG|MIN|MAX
    arg: Any  # ColRef | '*' string
    alias: str | None = None
    line: int = 1
    column: int = 1
    out_name: str | None = None


@dataclass
class SelectItem:
    expr: Any  # ColRef | Aggregate | '*'
    alias: str | None = None


@dataclass
class Compare:
    op: str
    left: Any
    right: Any
    line: int = 1
    column: int = 1


@dataclass
class Not:
    cond: Any


@dataclass
class And:
    left: Any
    right: Any


@dataclass
class Or:
    left: Any
    right: Any


@dataclass
class TableRef:
    kind: str  # 'base' | 'subquery'
    name: str | None = None  # base table
    alias: str | None = None
    subquery: Any = None  # Query
    line: int = 1
    column: int = 1


@dataclass
class JoinOn:
    left: TableRef
    right: TableRef
    condition: Any
    more: list = field(default_factory=list)  # [(TableRef, cond), ...]


@dataclass
class FromComma:
    tables: list  # list[TableRef]


@dataclass
class SortKey:
    expr: Any  # ColRef or alias name string after resolve
    direction: str = "asc"  # asc|desc
    # resolve fills:
    resolved: str | None = None


@dataclass
class SelectBlock:
    distinct: bool
    items: list  # SelectItem or special star
    from_clause: Any  # FromComma | JoinOn
    where: Any = None
    group_by: list = field(default_factory=list)  # ColRefs
    having: Any = None
    order_by: list = field(default_factory=list)
    limit: int | None = None
    line: int = 1
    column: int = 1
    # resolve fills:
    output_schema: list = field(default_factory=list)
    star_expanded: list = field(default_factory=list)


@dataclass
class SetOp:
    op: str  # union | union_all | intersect | except
    left: Any
    right: Any


# Query is SelectBlock | SetOp
Query = Any
Statement = Any
