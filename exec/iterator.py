"""Iterator interface for Module 2 operators."""

from __future__ import annotations

from abc import ABC, abstractmethod # abstract base class
from typing import Any


END = object()  # sentinel returned by next()


class ExecError(Exception):
    """Runtime / open-time execution error."""


class Operator(ABC):
    """Every physical operator implements open / next / close / counters."""

    def __init__(self):
        self._opened = False
        self._closed = False
        self.tuples_out = 0
        self.comparisons = 0
        self.page_reads = 0
        self.page_writes = 0
        self.schema: list = []  # [[qualified_name, type, ...], ...]
        self.attr_names: list[str] = []  # qualified names only
        self.children: list[Operator] = []
        self.frames_alloc: int = 1
        self.op_name: str = type(self).__name__

    @abstractmethod
    def open(self) -> None:
        ...

    @abstractmethod
    def next(self) -> Any:
        """Return next tuple (list of values) or END."""
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    def counters(self) -> dict:
        return {
            "tuples_out": self.tuples_out,
            "comparisons": self.comparisons,
            "page_reads": self.page_reads,
            "page_writes": self.page_writes,
        }

    def _emit(self, row: list) -> list:
        self.tuples_out += 1
        return row

    def _check_schema_unique(self, attrs: list[str]) -> None:
        seen = set()
        for a in attrs:
            if a in seen:
                raise ExecError(f"duplicate qualified column {a!r} in schema")
            seen.add(a)
