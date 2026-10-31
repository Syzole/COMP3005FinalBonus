from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol, Sequence, runtime_checkable

from engine.tokeniser import tokenise, TokenType

"""
In-memory relation storage, tuple-source protocol, and data.txt loader.

Operators consume RelationSource (attributes + iterable tuples), not a hard-wired
global catalog or a required list type. Table is one concrete source.
"""


@runtime_checkable
class RelationSource(Protocol):
    """Anything operators can pull tuples from (Module 2 will refine iterators)."""

    @property
    def attributes(self) -> list[str]: ...

    def __iter__(self) -> Iterator[Sequence]: ...


@dataclass
class Table:
    """Materialised bag of tuples. Implements RelationSource."""

    attributes: list[str]
    rows: list[list] = field(default_factory=list)

    def __iter__(self) -> Iterator[list]:
        return iter(self.rows)


def iter_tuples(source: RelationSource | Iterable) -> Iterator[list]:
    """Yield row lists from a RelationSource or a bare iterable of rows."""
    if isinstance(source, RelationSource) or hasattr(source, "attributes"):
        rows = getattr(source, "rows", source)
        for row in rows:
            yield list(row)
        return
    for row in source:
        yield list(row)


def materialize(source: RelationSource) -> Table:
    """Snapshot a source into a Table (needed when a child is scanned more than once)."""
    if isinstance(source, Table):
        return source
    attrs = list(source.attributes)
    return Table(attributes=attrs, rows=[list(r) for r in source])


class Catalog:
    """Name -> RelationSource. Passed into evaluate; operators never open files."""

    def __init__(self, tables: dict[str, RelationSource] | None = None):
        self._tables: dict[str, RelationSource] = dict(tables or {})

    def get(self, name: str) -> RelationSource | None:
        return self._tables.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tables

    def __getitem__(self, name: str) -> RelationSource:
        return self._tables[name]


def load_relation(text: str) -> dict[str, Table]:
    tokens = tokenise(text)
    i = 0
    tables: dict[str, Table] = {}

    def peek():
        return tokens[i]

    def next():
        nonlocal i
        t = tokens[i]
        i += 1
        return t

    def check(token_type, value=None):
        t = peek()
        if t.type != token_type:
            return False
        if value is not None and t.value != value:
            return False
        return True

    def expect(token_type, value=None):
        if not check(token_type, value):
            raise ValueError(
                f"Expected {token_type} but got {peek().type} at line {peek().line}, column {peek().column}"
            )
        return next()

    def read_value():
        if check(TokenType.NUMBER):
            return int(next().value)
        if check(TokenType.STRING):
            return next().value
        if check(TokenType.IDENTIFIER):
            return next().value
        raise ValueError(
            f"Expected a value but got {peek().type} at line {peek().line}, column {peek().column}"
        )

    while not check(TokenType.EOF):
        name = expect(TokenType.IDENTIFIER).value
        expect(TokenType.LPARENTHESIS)
        attributes = [expect(TokenType.IDENTIFIER).value]
        while check(TokenType.COMMA):
            next()
            attributes.append(expect(TokenType.IDENTIFIER).value)
        expect(TokenType.RPARENTHESIS)
        expect(TokenType.EQUALS)
        expect(TokenType.LBRACE)

        rows = []
        while not check(TokenType.RBRACE):
            row = [read_value()]
            while check(TokenType.COMMA):
                next()
                row.append(read_value())
            rows.append(row)

        expect(TokenType.RBRACE)

        seen_attrs = set()
        for attr in attributes:
            if attr in seen_attrs:
                raise ValueError(f"schema, duplicate attribute {attr}")
            seen_attrs.add(attr)

        # Bag semantics on load (no dedup).
        tables[name] = Table(attributes, rows)
    return tables
