"""union, intersect, except (set semantics via sort)."""

from __future__ import annotations

from exec.iterator import Operator, END, ExecError
from exec.ops_scan import UnionAll
from exec.ops_agg import Distinct
from exec.ops_sort import Sort
from exec.util import attr_names, check_set_compatible, compare_rows


class Union(Operator): #union all then sort and remove dupes, output names come from left side
    def __init__(self, ctx, left: Operator, right: Operator, frames=None):
        super().__init__()
        self.ctx = ctx
        self.left = left
        self.right = right
        self.children = [left, right]
        self.op_name = "union"
        self.frames_alloc = frames if frames is not None else max(3, ctx.B // 2)
        self._inner = None

    def open(self) -> None:
        ua = UnionAll(self.left, self.right)
        self._inner = Distinct(self.ctx, ua, mode="sort", frames=self.frames_alloc)
        self._inner.open()
        self.schema = [list(c) for c in self._inner.schema]
        self.attr_names = attr_names(self.schema)
        self.children = [self._inner]
        self._opened = True

    def next(self):
        t = self._inner.next()
        if t is END:
            return END
        return self._emit(t)

    def close(self) -> None:
        self._inner.close()
        self._closed = True


class Intersect(Operator): #sort both sides then merge and keep dupes
    def __init__(self, ctx, left: Operator, right: Operator, frames=None):
        super().__init__()
        self.ctx = ctx
        self.left = left
        self.right = right
        self.children = [left, right]
        self.op_name = "intersect"
        self.frames_alloc = frames if frames is not None else max(3, ctx.B // 2)
        self._out = []
        self._i = 0

    def open(self) -> None:
        self.left.open()
        left_schema = [list(c) for c in self.left.schema]
        self.left.close()
        self.right.open()
        right_schema = [list(c) for c in self.right.schema]
        self.right.close()
        check_set_compatible(left_schema, right_schema)
        self.schema = left_schema
        self.attr_names = attr_names(self.schema)

        keys = [[c[0], "asc"] for c in left_schema]
        half = max(3, self.frames_alloc // 2)
        # distinct+sort each side
        dl = Distinct(self.ctx, self.left, "sort", frames=half)
        dr = Distinct(self.ctx, self.right, "sort", frames=half)
        dl.open()
        dr.open()

        a = dl.next()
        b = dr.next()
        out = []
        # both inputs are sorted and unique
        #smaller side advances
        while a is not END and b is not END:
            self.comparisons += 1
            c = compare_rows(a, b)
            if c == 0:
                out.append(a)
                a = dl.next()
                b = dr.next()
            elif c < 0:
                a = dl.next()
            else:
                b = dr.next()
        dl.close()
        dr.close()
        self.page_reads += dl.page_reads + dr.page_reads
        self.page_writes += dl.page_writes + dr.page_writes
        self._out = out
        self._i = 0
        self._opened = True

    def next(self):
        if self._i >= len(self._out):
            return END
        row = self._out[self._i]
        self._i += 1
        return self._emit(row)

    def close(self) -> None:
        self._closed = True


class Except(Operator): #same as intersect but keep only the tuples from the left side
    def __init__(self, ctx, left: Operator, right: Operator, frames=None):
        super().__init__()
        self.ctx = ctx
        self.left = left
        self.right = right
        self.children = [left, right]
        self.op_name = "except"
        self.frames_alloc = frames if frames is not None else max(3, ctx.B // 2)
        self._out = []
        self._i = 0

    def open(self) -> None:
        self.left.open()
        left_schema = [list(c) for c in self.left.schema]
        self.left.close()
        self.right.open()
        right_schema = [list(c) for c in self.right.schema]
        self.right.close()
        check_set_compatible(left_schema, right_schema)
        self.schema = left_schema
        self.attr_names = attr_names(self.schema)

        half = max(3, self.frames_alloc // 2)
        dl = Distinct(self.ctx, self.left, "sort", frames=half)
        dr = Distinct(self.ctx, self.right, "sort", frames=half)
        dl.open()
        dr.open()

        a = dl.next()
        b = dr.next()
        out = []
        #equal keys are skipped, left side advances
        # when the right side is done, every remaining left side tuple is added to the output
        while a is not END:
            if b is END:
                out.append(a)
                a = dl.next()
                continue
            self.comparisons += 1
            c = compare_rows(a, b)
            if c == 0:
                a = dl.next()
                b = dr.next()
            elif c < 0:
                out.append(a)
                a = dl.next()
            else:
                b = dr.next()
        dl.close()
        dr.close()
        self.page_reads += dl.page_reads + dr.page_reads
        self.page_writes += dl.page_writes + dr.page_writes
        self._out = out
        self._i = 0
        self._opened = True

    def next(self):
        if self._i >= len(self._out):
            return END
        row = self._out[self._i]
        self._i += 1
        return self._emit(row)

    def close(self) -> None:
        self._closed = True
