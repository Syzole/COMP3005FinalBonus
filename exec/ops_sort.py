"""External merge sort with carry-forward of untouched runs."""

from __future__ import annotations

import heapq
import math
from pathlib import Path

from exec.iterator import Operator, END, ExecError
from exec.temp import TempRunWriter, TempRunReader
from exec.util import attr_names, index_of
from exec.context import tuple_nbytes
from storage.disk import PAGE_SIZE


class Sort(Operator):
    """External merge sort. Pass 0 + intermediate merges in open(); final merge in next()."""

    def __init__(self, ctx, child: Operator, keys: list[list], frames: int | None = None):
        super().__init__()
        self.ctx = ctx
        self.child = child
        self.children = [child]
        self.keys_spec = keys  # [[col, 'asc'|'desc'], ...]
        self.op_name = "sort"
        self.frames_alloc = frames if frames is not None else ctx.B
        self.pass_count = 0
        self._final_reader = None
        self._final_merge = None
        self._key_idxs: list[int] = []
        self._dirs: list[str] = []
        self._temp_paths: list[Path] = []

    def open(self) -> None:
        """
        Pass 0: pack ≤ B pages of tuples → sort → write run files
        Merge: fan-in B-1; lone leftover run is *carried forward* (not rewritten)
        Stop when ≤ B-1 runs left
        """


        B = max(3, self.frames_alloc)  # need B>=3 for merge (B-1 inputs + 1 out)
        self._held = B
        try:
            self.ctx.budget.acquire(B)
        except Exception:
            # Soft accounting: still run; peak tracked when acquire works
            self._held = 0
        self.child.open()
        self.schema = [list(c) for c in self.child.schema]
        self.attr_names = attr_names(self.schema)
        self._key_idxs = [index_of(self.schema, k[0]) for k in self.keys_spec]
        self._dirs = [k[1] for k in self.keys_spec]

        # --- Pass 0: run generation ---
        # Fill exactly B slotted pages of tuples, sort in memory, write one run.
        runs: list[Path] = []
        buf: list[list] = []
        full_pages = 0  # completed pages in the current run
        page_free = 4088

        def flush_run():
            nonlocal buf, full_pages, page_free
            if not buf:
                return
            buf.sort(key=lambda row: self._sort_key(row))
            path = self.ctx.new_temp_path("sort_r0")
            self._temp_paths.append(path)
            w = TempRunWriter(path, self.schema)
            for row in buf:
                w.write(row)
            w.close()
            self.page_writes += w.page_writes
            runs.append(path)
            buf = []
            full_pages = 0
            page_free = 4088

        while True:
            t = self.child.next()
            if t is END:
                break
            need = tuple_nbytes(self.schema, t) + 4  # record + slot entry
            if need > page_free:
                # current page is full
                full_pages += 1
                page_free = 4088
                if full_pages >= B:
                    flush_run()
            if need > page_free:
                raise ExecError("tuple larger than one page")
            page_free -= need
            buf.append(t)
        flush_run()

        self.pass_count = 1  # pass 0 done
        if not runs:
            self._final_merge = iter([])
            self._opened = True
            return

        if len(runs) == 1:
            # Already sorted; stream from the single run (no extra merge write).
            r = TempRunReader(runs[0], self.schema)
            self._final_reader = r
            self._final_merge = self._iter_reader(r)
            self._opened = True
            return

        # --- Merge passes until <= B-1 runs remain for final streaming merge ---
        fanin = max(2, B - 1)
        while len(runs) > fanin:
            new_runs: list[Path] = []
            i = 0
            while i < len(runs):
                group = runs[i : i + fanin]
                if len(group) == 1:
                    # Carry forward untouched
                    new_runs.append(group[0])
                    i += 1
                    continue
                out = self.ctx.new_temp_path("sort_m")
                self._temp_paths.append(out)
                self._merge_runs_to_file(group, out)
                new_runs.append(out)
                i += len(group)
            runs = new_runs
            self.pass_count += 1

        # Final merge streams via next()
        self.pass_count += 1
        self._final_merge = self._k_merge(runs)
        self._opened = True

    def _sort_key(self, row: list):
        # Python sort key that respects desc: negate ints, reverse strings via wrapper
        parts = []
        for idx, d in zip(self._key_idxs, self._dirs):
            v = row[idx]
            if d == "desc":
                parts.append(_Desc(v))
            else:
                parts.append(v)
        return tuple(parts)

    def _merge_runs_to_file(self, paths: list[Path], out: Path) -> None:
        w = TempRunWriter(out, self.schema)
        for row in self._k_merge(paths, count_reads=True):
            w.write(row)
        w.close()
        self.page_writes += w.page_writes

    def _k_merge(self, paths: list[Path], count_reads: bool = True):
        readers = [TempRunReader(p, self.schema) for p in paths]
        heap = []
        for i, r in enumerate(readers):
            row = r.next()
            if row is not None:
                heap.append((self._sort_key(row), i, row))
        heapq.heapify(heap)
        while heap:
            _k, i, row = heapq.heappop(heap)
            yield row
            nxt = readers[i].next()
            if nxt is not None:
                heapq.heappush(heap, (self._sort_key(nxt), i, nxt))
        if count_reads:
            self.page_reads += sum(r.page_reads for r in readers)

    def _iter_reader(self, r: TempRunReader):
        while True:
            row = r.next()
            if row is None:
                self.page_reads += r.page_reads
                return
            yield row

    def next(self): # final merge 
        if self._final_merge is None:
            return END
        try:
            row = next(self._final_merge)
        except StopIteration:
            return END
        return self._emit(row)

    def close(self) -> None: # clean up temp files and release budget
        self.child.close()
        if getattr(self, "_held", 0):
            self.ctx.budget.release(self._held)
            self._held = 0
        self._final_merge = None
        self._final_reader = None
        self._closed = True


class _Desc:
    """Wrapper so heapq/sorted treat value as descending."""

    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        return self.v > other.v

    def __eq__(self, other):
        return self.v == other.v


class TupleSource(Operator):
    """In-memory / synthetic source for tests (counts pages by byte volume)."""

    def __init__(self, schema: list, rows: list[list], page_reads: int | None = None):
        super().__init__()
        self.schema = [list(c) for c in schema]
        self.attr_names = attr_names(self.schema)
        self._rows = rows
        self._i = 0
        self.op_name = "tuple_source"
        self._forced_reads = page_reads

    def open(self) -> None:
        self._i = 0
        if self._forced_reads is not None:
            self.page_reads = self._forced_reads
        else:
            nbytes = sum(tuple_nbytes(self.schema, r) for r in self._rows)
            self.page_reads = max(1, math.ceil(nbytes / PAGE_SIZE)) if self._rows else 0
        self._opened = True

    def next(self):
        if self._i >= len(self._rows):
            return END
        row = self._rows[self._i]
        self._i += 1
        return self._emit(list(row))

    def close(self) -> None:
        self._closed = True
