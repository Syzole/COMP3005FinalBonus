"""distinct and group_by (sort-based and hash-based)."""

from __future__ import annotations

from collections import defaultdict

from exec.iterator import Operator, END, ExecError
from exec.ops_sort import Sort
from exec.temp import TempRunWriter, TempRunReader
from exec.util import attr_names, index_of, compare_rows
from exec.context import tuple_nbytes
from storage.disk import PAGE_SIZE


class Distinct(Operator): #sort then drop adjacent equals if mode is sort
    # if mode is hash, keep track of seen tuples in memory, and spill to disk if exceeds frames_alloc, then read back and dedupe in memory
    def __init__(self, ctx, child: Operator, mode: str, frames: int | None = None):
        super().__init__()
        self.ctx = ctx
        self.child = child
        self.children = [child]
        self.mode = mode
        self.op_name = "distinct"
        self.frames_alloc = frames if frames is not None else max(3, ctx.B // 2)
        self._inner: Operator | None = None
        self._prev = None
        self._hash_iter = None

    def open(self) -> None:
        if self.mode == "sort":
            # Sort needs keys before open, so learn the schema, close, then sort on every column.
            self.child.open()
            schema = [list(c) for c in self.child.schema]
            self.child.close()
            keys = [[c[0], "asc"] for c in schema]
            self._inner = Sort(self.ctx, self.child, keys, frames=self.frames_alloc)
            self._inner.open()
            self.schema = [list(c) for c in self._inner.schema]
            self.attr_names = attr_names(self.schema)
            self._prev = None
            self.children = [self._inner]
        else:
            self._open_hash()
        self._opened = True

    def _open_hash(self) -> None:
        """Partition when seen-set exceeds frames; dedupe partition by partition."""
        B = max(3, self.frames_alloc)
        self.child.open()
        self.schema = [list(c) for c in self.child.schema]
        self.attr_names = attr_names(self.schema)
        capacity = B * PAGE_SIZE
        seen: dict[tuple, None] = {}
        seen_bytes = 0
        spilled = False
        parts: list = []  # list of TempRunWriter paths for overflow partitions

        P = max(2, B - 1)
        writers = None

        def ensure_writers():
            nonlocal writers, spilled
            if writers is not None:
                return
            spilled = True
            writers = []
            for _ in range(P):
                path = self.ctx.new_temp_path("dist_p")
                w = TempRunWriter(path, self.schema)
                writers.append((path, w))

        while True:
            t = self.child.next()
            if t is END:
                break
            key = tuple(t)
            nb = tuple_nbytes(self.schema, t)
            if not spilled and seen_bytes + nb <= capacity:
                if key not in seen:
                    seen[key] = None
                    seen_bytes += nb
            else:
                ensure_writers()
                # spill current seen into partition 0 file as seed? Simpler: spill all
                # incoming via hash, including re-emitting seen into partitions.
                if seen:
                    for k in seen:
                        row = list(k)
                        h = hash(k) % P
                        writers[h][1].write(row)
                    seen.clear()
                    seen_bytes = 0
                h = hash(key) % P
                writers[h][1].write(list(t))

        out_rows: list[list] = []
        if not spilled:
            out_rows = [list(k) for k in seen.keys()]
        else:
            for path, w in writers:
                w.close()
                self.page_writes += w.page_writes
                # dedupe each partition in memory if fits, else sort-distinct
                r = TempRunReader(path, self.schema)
                local: dict[tuple, None] = {}
                while True:
                    row = r.next()
                    if row is None:
                        break
                    local[tuple(row)] = None
                self.page_reads += r.page_reads
                out_rows.extend(list(k) for k in local.keys())

        self._hash_iter = iter(out_rows)
        self.children = [self.child]

    def next(self):
        if self.mode == "sort":
            while True:
                t = self._inner.next()
                if t is END:
                    return END
                self.comparisons += 1
                if self._prev is not None and compare_rows(t, self._prev) == 0:
                    continue
                self._prev = t
                return self._emit(t)
        else:
            try:
                row = next(self._hash_iter)
            except StopIteration:
                return END
            return self._emit(row)

    def close(self) -> None:
        if self.mode == "sort" and self._inner is not None:
            self._inner.close()
            # Sort already closed child; I/O lives on _inner (walked as child)
        else:
            self.child.close()
        self._closed = True


def _agg_out_name(fn: str, col: str) -> str:
    fn = fn.lower()
    if fn == "count" and col == "*":
        return "count_star"
    safe = col.replace(".", "_")
    return f"{fn}_{safe}"


class GroupBy(Operator):# sort mode is check adjacent groups, hash mode uses keys
    def __init__(
        self, ctx, child: Operator, mode: str, keys: list[str], aggregates: list, frames=None
    ):
        super().__init__()
        self.ctx = ctx
        self.child = child
        self.children = [child]
        self.mode = mode
        self.keys = keys
        self.aggregates = aggregates  # [[fn, col], ...]
        self.op_name = "group_by"
        self.frames_alloc = frames if frames is not None else max(3, ctx.B // 2)
        self._out_iter = None
        self._inner = None

    def open(self) -> None:
        # Build output schema after seeing child schema
        if self.mode == "sort":
            self.child.open()
            child_schema = [list(c) for c in self.child.schema]
            self.child.close()
            sort_keys = [[k, "asc"] for k in self.keys] if self.keys else [
                [child_schema[0][0], "asc"]
            ] if child_schema and False else []
            # If no group keys, we don't need sort — single group. Still open child once.
            if self.keys:
                sort_keys = [[k, "asc"] for k in self.keys]
                self._inner = Sort(self.ctx, self.child, sort_keys, frames=self.frames_alloc)
                self._inner.open()
                src = self._inner
            else:
                self.child.open()
                src = self.child
            self._build_schema(src.schema)
            rows = self._aggregate_sorted(src)
            self._out_iter = iter(rows)
        else:
            self.child.open()
            self._build_schema(self.child.schema)
            rows = self._aggregate_hash()
            self._out_iter = iter(rows)
        self._opened = True

    def _build_schema(self, child_schema: list) -> None:
        self._key_idxs = [index_of(child_schema, k) for k in self.keys]
        self._child_schema = child_schema
        self._child_attrs = attr_names(child_schema)
        out = []
        for k in self.keys:
            out.append(list(child_schema[index_of(child_schema, k)]))
        for spec in self.aggregates:
            fn, col = spec[0], spec[1]
            name = spec[2] if len(spec) > 2 else _agg_out_name(fn, col)
            fnl = fn.lower()
            if fnl in ("sum", "count"):
                out.append([name, "INT"])
            elif fnl == "avg":
                out.append([name, "VARCHAR", 32])  # print as string 4 decimals — use float stored as str? Spec says real printed to 4 decimals. Store float.
                # Use a pseudo type — we'll store float and JSON will print it.
                out[-1] = [name, "FLOAT"]
            else:  # min max — type of column
                if col == "*":
                    raise ExecError("MIN/MAX require a column")
                src = child_schema[index_of(child_schema, col)]
                out.append([name] + list(src[1:]))
        self.schema = out
        self.attr_names = attr_names(self.schema)

    def _aggregate_sorted(self, src: Operator) -> list[list]:
        groups: list[list] = []
        cur_key = object()
        cur_rows: list[list] = []

        def flush():
            if cur_rows:
                groups.append(self._reduce(cur_rows))

        while True:
            t = src.next()
            if t is END:
                flush()
                break
            k = tuple(t[i] for i in self._key_idxs) if self._key_idxs else ()
            if k != cur_key:
                flush()
                cur_key = k
                cur_rows = [t]
            else:
                cur_rows.append(t)

        if not self.keys and not groups:
            # empty input, no grouping columns → one row
            return [self._reduce([])]
        return groups

    def _aggregate_hash(self) -> list[list]:
        groups: dict[tuple, list] = defaultdict(list)
        n = 0
        while True:
            t = self.child.next()
            if t is END:
                break
            n += 1
            k = tuple(t[i] for i in self._key_idxs) if self._key_idxs else ()
            groups[k].append(t)
            # Memory check: if exceeds budget, spill (simplified: fall back to sort path)
            nbytes = sum(tuple_nbytes(self._child_schema, r) for rows in groups.values() for r in rows)
            if nbytes > self.frames_alloc * PAGE_SIZE:
                # group tables too big, spill to disk, then read back and sort
                path = self.ctx.new_temp_path("gb_spill")
                w = TempRunWriter(path, self._child_schema)
                for rows in groups.values():
                    for r in rows:
                        w.write(r)
                while True:
                    t = self.child.next()
                    if t is END:
                        break
                    w.write(t)
                w.close()
                self.page_writes += w.page_writes
                self.child.close()
                # Replace with sort-based from temp
                from exec.ops_sort import TupleSource
                # Read all back (could be large) — better rebuild Sort over reader operator
                r = TempRunReader(path, self._child_schema)
                rows_all = []
                while True:
                    row = r.next()
                    if row is None:
                        break
                    rows_all.append(row)
                self.page_reads += r.page_reads
                src = TupleSource(self._child_schema, rows_all)
                if self.keys:
                    s = Sort(self.ctx, src, [[k, "asc"] for k in self.keys], frames=self.frames_alloc)
                    s.open()
                    return self._aggregate_sorted(s)
                src.open()
                return self._aggregate_sorted(src)

        if not self.keys and n == 0:
            return [self._reduce([])]
        return [self._reduce(rows) for rows in groups.values()]

    def _reduce(self, rows: list[list]) -> list:
        #one output row: group key, then aggregate values
        out = []
        if rows and self._key_idxs:
            out.extend(rows[0][i] for i in self._key_idxs)
        elif self._key_idxs:
            out.extend([None] * len(self._key_idxs))

        for spec in self.aggregates:
            fn, col = spec[0], spec[1]
            fnl = fn.lower()
            if fnl == "count":
                if col == "*":
                    out.append(len(rows))
                else:
                    idx = index_of(self._child_schema, col)
                    out.append(sum(1 for r in rows if r[idx] is not None))
            elif fnl == "sum":
                if not rows:
                    raise ExecError("SUM undefined on empty ungrouped input")
                idx = index_of(self._child_schema, col)
                out.append(sum(r[idx] for r in rows))
            elif fnl == "avg":
                if not rows:
                    raise ExecError("AVG undefined on empty ungrouped input")
                idx = index_of(self._child_schema, col)
                vals = [r[idx] for r in rows]
                avg = sum(vals) / len(vals)
                out.append(float(f"{avg:.4f}"))
            elif fnl == "min":
                if not rows:
                    raise ExecError("MIN undefined on empty ungrouped input")
                idx = index_of(self._child_schema, col)
                out.append(min(r[idx] for r in rows))
            elif fnl == "max":
                if not rows:
                    raise ExecError("MAX undefined on empty ungrouped input")
                idx = index_of(self._child_schema, col)
                out.append(max(r[idx] for r in rows))
            else:
                raise ExecError(f"unknown aggregate {fn}")
        return out

    def next(self):
        try:
            row = next(self._out_iter)
        except StopIteration:
            return END
        return self._emit(row)

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()
        else:
            try:
                self.child.close()
            except Exception:
                pass
        if self._inner:
            self.page_reads += self._inner.page_reads
            self.page_writes += self._inner.page_writes
        self._closed = True
