"""
seqscan, filter, project, limit, union_all.

seqscan: scan a table from the heap
filter: comparisons += 1 per tuple looked at (not per AND/OR inside)
project: bag project - no distinct tuples allowed
union_all: merge left then right, and we keep dupes
"""

from __future__ import annotations

from engine.expr import parse_condition, matches
from exec.iterator import Operator, END, ExecError
from exec.util import attr_names, index_of, check_set_compatible


class SeqScan(Operator): # one data page at a time, heap order
    def __init__(self, ctx, table: str, alias: str | None = None):
        super().__init__()
        self.ctx = ctx
        self.table = table
        self.alias = alias  # optional; qualifies columns as alias.col
        self.op_name = "seqscan"
        self._heap = None
        self._pool = None
        self._iter = None
        self._disk_reads0 = 0
        self._disk_writes0 = 0

    def open(self) -> None: # Output names are alias.col if "as" was set, otherwise table.col.
        heap, pool, info = self.ctx.open_table(self.table, pool_frames=2)
        self._heap = heap
        self._pool = pool
        qual = self.alias if self.alias else self.table
        # Re-qualify from bare heap schema so alias replaces table name.
        schema = []
        for col in info["schema"]:
            cname = col[0].split(".")[-1]
            schema.append([f"{qual}.{cname}"] + list(col[1:]))
        self.schema = schema
        self.attr_names = attr_names(self.schema)
        self._check_schema_unique(self.attr_names)
        self._disk_reads0 = pool.disk.reads
        self._disk_writes0 = pool.disk.writes
        self._iter = heap.scan()
        self._opened = True

    def next(self):
        try:
            _rid, tup = next(self._iter)
        except StopIteration:
            return END
        # tup uses unqualified heap schema values; same order as qschema
        return self._emit(list(tup))

    def close(self) -> None:
        if self._pool is not None:
            self.page_reads = self._pool.disk.reads - self._disk_reads0
            self.page_writes = self._pool.disk.writes - self._disk_writes0
            self._pool.flush_all()
        self._iter = None
        self._closed = True


class Filter(Operator): # comparisons += 1 per tuple looked at (not per AND/OR inside)
    def __init__(self, child: Operator, condition: str):
        super().__init__()
        self.child = child
        self.children = [child]
        self.condition = condition
        self.op_name = "filter"
        self._cond = None

    def open(self) -> None:
        self.child.open()
        self.schema = [list(c) for c in self.child.schema]
        self.attr_names = attr_names(self.schema)
        try:
            self._cond = parse_condition(self.condition)
        except Exception as e:
            raise ExecError(f"bad filter condition: {e}") from e
        # Validate columns exist by probing empty? Dry-run against schema names.
        self._opened = True

    def next(self):
        while True:
            t = self.child.next()
            if t is END:
                return END
            self.comparisons += 1  # one per tuple examined
            if matches(self._cond, self.attr_names, t):
                return self._emit(t)

    def close(self) -> None:
        self.child.close()
        self._closed = True


class Project(Operator): # keeps listed cols in order listed, does not remove dupes
    def __init__(self, child: Operator, columns: list[str]):
        super().__init__()
        self.child = child
        self.children = [child]
        self.columns = columns
        self.op_name = "project"
        self._idxs: list[int] = []

    def open(self) -> None:
        self.child.open()
        self._idxs = []
        out_schema = []
        for col in self.columns:
            i = index_of(self.child.schema, col)
            self._idxs.append(i)
            out_schema.append(list(self.child.schema[i]))
        self.schema = out_schema
        self.attr_names = attr_names(self.schema)
        self._check_schema_unique(self.attr_names)
        self._opened = True

    def next(self):
        t = self.child.next()
        if t is END:
            return END
        return self._emit([t[i] for i in self._idxs])

    def close(self) -> None:
        self.child.close()
        self._closed = True


class Limit(Operator): #returns the first n tuples, then stops
    def __init__(self, child: Operator, n: int):
        super().__init__()
        self.child = child
        self.children = [child]
        self.n = n
        self.op_name = "limit"
        self._sent = 0

    def open(self) -> None:
        self.child.open()
        self.schema = [list(c) for c in self.child.schema]
        self.attr_names = attr_names(self.schema)
        self._sent = 0
        self._opened = True

    def next(self):
        if self._sent >= self.n:
            return END
        t = self.child.next()
        if t is END:
            return END
        self._sent += 1
        return self._emit(t)

    def close(self) -> None:
        self.child.close()
        self._closed = True


class UnionAll(Operator): #merge left then right
    def __init__(self, left: Operator, right: Operator):
        super().__init__()
        self.left = left
        self.right = right
        self.children = [left, right]
        self.op_name = "union_all"
        self._phase = 0

    def open(self) -> None:
        self.left.open()
        self.right.open()
        check_set_compatible(self.left.schema, self.right.schema)
        self.schema = [list(c) for c in self.left.schema]
        self.attr_names = attr_names(self.schema)
        self._phase = 0
        self._opened = True

    def next(self):
        if self._phase == 0:
            t = self.left.next()
            if t is not END:
                return self._emit(t)
            self._phase = 1
        t = self.right.next()
        if t is END:
            return END
        return self._emit(t)

    def close(self) -> None:
        self.left.close()
        self.right.close()
        self._closed = True
