"""Three join algorithms: nested_loop, sort_merge, hash."""

from __future__ import annotations

from collections import defaultdict

from engine.expr import parse_condition, matches
from exec.iterator import Operator, END, ExecError
from exec.ops_sort import Sort
from exec.temp import TempRunWriter, TempRunReader, drain_to_temp
from exec.util import (
    attr_names, merge_schemas, extract_eq_keys, key_tuple, cmp_keys,
)
from exec.context import tuple_nbytes
from storage.disk import PAGE_SIZE


class NestedLoopJoin(Operator):
    """
    Block nested loop. Outer block is B-2 pages, one frame for the inner page.
    Right input is written once so each outer block can rescan it.
    comparisons is |R| * |S|: one per pair, match or not.
    """

    def __init__(self, ctx, left: Operator, right: Operator, condition: str, frames=None):
        super().__init__()
        self.ctx = ctx
        self.left = left
        self.right = right
        self.children = [left, right]
        self.condition = condition
        self.op_name = "nested_loop_join"
        self.frames_alloc = frames if frames is not None else ctx.B
        self._cond = None
        self._out_buf: list[list] = []
        self._out_i = 0
        self._done = False
        self._inner_path = None
        self._outer_count = 0
        self._inner_count = 0

    def open(self) -> None:
        B = max(4, self.frames_alloc)
        block_frames = max(1, B - 2)
        self.left.open()
        self.right.open()
        self.schema = merge_schemas(self.left.schema, self.right.schema)
        self.attr_names = attr_names(self.schema)
        try:
            self._cond = parse_condition(self.condition)
        except Exception as e:
            raise ExecError(f"bad join condition: {e}") from e

        # Materialize right so we can rescan per outer block
        self._inner_path = self.ctx.new_temp_path("nlj_inner")
        w = drain_to_temp(self.right, self.right.schema, self._inner_path)
        self.page_writes += w.page_writes
        self._inner_count = w.num_tuples
        inner_pages = max(1, w.num_pages()) if w.num_tuples else 0

        # Process outer in blocks
        capacity = block_frames * PAGE_SIZE
        block: list[list] = []
        block_bytes = 0
        self._out_buf = []

        def process_block(blk: list[list]):
            if not blk:
                return
            reader = TempRunReader(self._inner_path, self.right.schema)
            while True:
                s = reader.next()
                if s is None:
                    break
                for r in blk:
                    self.comparisons += 1
                    joined = list(r) + list(s)
                    if matches(self._cond, self.attr_names, joined):
                        self._out_buf.append(joined)
            self.page_reads += reader.page_reads

        # Count outer while blocking
        while True:
            t = self.left.next()
            if t is END:
                break
            self._outer_count += 1
            nb = tuple_nbytes(self.left.schema, t)
            if block and block_bytes + nb > capacity:
                process_block(block)
                block = []
                block_bytes = 0
            block.append(t)
            block_bytes += nb
        process_block(block)

        # If outer empty, still need |R|*|S| = 0 comparisons — OK
        # If we never scanned inner because outer empty, comparisons = 0 = 0*|S|
        # Spec: comparisons = |R|*|S| always. If outer empty we didn't compare —
        # that's correct (0). If outer non-empty we compared every pair.

        self._out_i = 0
        self._done = False
        self._opened = True

    def next(self):
        if self._out_i >= len(self._out_buf):
            return END
        row = self._out_buf[self._out_i]
        self._out_i += 1
        return self._emit(row)

    def close(self) -> None:
        self.left.close()
        self.right.close()
        self._closed = True


class SortMergeJoin(Operator):
    """
    Sort both sides on the join key, then merge.
    On equal keys, rewind the right run so every left duplicate pairs with every right duplicate.
    A right run bigger than B-2 pages is spilled to a temp file and reread per left row
    """
    def __init__(self, ctx, left: Operator, right: Operator, condition: str, frames=None):
        super().__init__()
        self.ctx = ctx
        self.left = left
        self.right = right
        self.children = [left, right]
        self.condition = condition
        self.op_name = "sort_merge_join"
        self.frames_alloc = frames if frames is not None else ctx.B
        self._out_buf: list[list] = []
        self._out_i = 0

    def open(self) -> None:
        B = max(3, self.frames_alloc)
        # Need schemas: open children briefly via Sort
        # Extract keys after left/right schemas known — open Sort which opens children.
        # Probe: create sorts after getting schemas.
        self.left.open()
        left_schema = [list(c) for c in self.left.schema]
        self.left.close()
        self.right.open()
        right_schema = [list(c) for c in self.right.schema]
        self.right.close()

        self.schema = merge_schemas(left_schema, right_schema)
        self.attr_names = attr_names(self.schema)
        li, ri = extract_eq_keys(self.condition, left_schema, right_schema)

        left_keys = [[left_schema[i][0], "asc"] for i in li]
        right_keys = [[right_schema[i][0], "asc"] for i in ri]
        half = max(3, B // 2)
        sl = Sort(self.ctx, self.left, left_keys, frames=half)
        sr = Sort(self.ctx, self.right, right_keys, frames=half)
        sl.open()
        sr.open()

        # Materialize sorted streams to allow rewinding S equal-key runs
        left_path = self.ctx.new_temp_path("smj_l")
        right_path = self.ctx.new_temp_path("smj_r")
        wl = drain_to_temp(sl, left_schema, left_path)
        wr = drain_to_temp(sr, right_schema, right_path)
        self.page_writes += wl.page_writes + wr.page_writes
        self.page_reads += sl.page_reads + sr.page_reads
        self.page_writes += sl.page_writes + sr.page_writes
        self.comparisons += sl.comparisons + sr.comparisons
        sl.close()
        sr.close()

        # Load right into list of rows for rewind, spilling equal runs if huge
        R = TempRunReader(left_path, left_schema)
        S = TempRunReader(right_path, right_schema)
        self._out_buf = []

        r = R.next()
        s = S.next()
        while r is not None and s is not None:
            rk = key_tuple(r, li)
            sk = key_tuple(s, ri)
            self.comparisons += 1
            c = cmp_keys(rk, sk)
            if c < 0:
                r = R.next()
            elif c > 0:
                s = S.next()
            else:
                # Collect S run with this key — spill to temp if larger than budget
                s_run: list[list] = []
                s_run_path = None
                s_run_bytes = 0
                capacity = max(1, B - 2) * PAGE_SIZE
                mark_key = sk
                spill_writer = None
                while s is not None and key_tuple(s, ri) == mark_key:
                    nb = tuple_nbytes(right_schema, s)
                    if spill_writer is None and s_run and s_run_bytes + nb > capacity:
                        s_run_path = self.ctx.new_temp_path("smj_eq")
                        spill_writer = TempRunWriter(s_run_path, right_schema)
                        for row in s_run:
                            spill_writer.write(row)
                        s_run = []
                    if spill_writer is not None:
                        spill_writer.write(s)
                    else:
                        s_run.append(s)
                        s_run_bytes += nb
                    s = S.next()
                if spill_writer is not None:
                    spill_writer.close()
                    self.page_writes += spill_writer.page_writes

                while r is not None and key_tuple(r, li) == mark_key:
                    if s_run_path is not None:
                        rd = TempRunReader(s_run_path, right_schema)
                        while True:
                            s2 = rd.next()
                            if s2 is None:
                                break
                            self._out_buf.append(list(r) + list(s2))
                        self.page_reads += rd.page_reads
                    else:
                        for s2 in s_run:
                            self._out_buf.append(list(r) + list(s2))
                    r = R.next()

        self.page_reads += R.page_reads + S.page_reads
        self._out_i = 0
        self._opened = True

    def next(self):
        if self._out_i >= len(self._out_buf):
            return END
        row = self._out_buf[self._out_i]
        self._out_i += 1
        return self._emit(row)

    def close(self) -> None:
        self._closed = True


class HashJoin(Operator):
    """Grace hash join: partition both sides (P = B-1), then build/probe per
    partition. If a build partition exceeds B-1 pages, fall back to BNL on that pair.
    """

    def __init__(self, ctx, left: Operator, right: Operator, condition: str, frames=None):
        super().__init__()
        self.ctx = ctx
        self.left = left  # build side
        self.right = right  # probe side
        self.children = [left, right]
        self.condition = condition
        self.op_name = "hash_join"
        self.frames_alloc = frames if frames is not None else ctx.B
        self._out_buf: list[list] = []
        self._out_i = 0
        self.overflow_partitions = 0

    def open(self) -> None:
        B = max(4, self.frames_alloc)
        P = max(2, B - 1)
        self.left.open()
        self.right.open()
        left_schema = [list(c) for c in self.left.schema]
        right_schema = [list(c) for c in self.right.schema]
        self.schema = merge_schemas(left_schema, right_schema)
        self.attr_names = attr_names(self.schema)
        li, ri = extract_eq_keys(self.condition, left_schema, right_schema)

        left_paths = self._partition(self.left, left_schema, li, P, "hj_l")
        right_paths = self._partition(self.right, right_schema, ri, P, "hj_r")

        # Hash build may hold up to B-1 pages of tuples; BNL uses B-2 for the outer block.
        build_limit = max(1, B - 1) * PAGE_SIZE
        block_capacity = max(1, B - 2) * PAGE_SIZE
        self._out_buf = []
        for i in range(P):
            self._join_partition(
                left_paths[i], right_paths[i],
                left_schema, right_schema, li, ri,
                build_limit, block_capacity,
            )

        self._out_i = 0
        self._opened = True

    def _partition(
        self, child: Operator, schema: list, key_idxs: list[int], P: int, prefix: str
    ) -> list:
        """Drain child into P temp files by hash(key) % P. Returns paths."""
        writers = []
        paths = []
        for i in range(P):
            path = self.ctx.new_temp_path(f"{prefix}{i}")
            paths.append(path)
            writers.append(TempRunWriter(path, schema))

        while True:
            t = child.next()
            if t is END:
                break
            writers[hash(key_tuple(t, key_idxs)) % P].write(t)

        for w in writers:
            w.close()
            self.page_writes += w.page_writes
        return paths

    def _try_load_build(
        self, path, schema: list, build_limit: int
    ) -> list[list] | None:
        """Load build partition if it fits in build_limit bytes; else None (overflow).

        On overflow we discard any partial buffer — caller runs BNL from the file.
        """
        reader = TempRunReader(path, schema)
        rows: list[list] = []
        build_bytes = 0
        while True:
            row = reader.next()
            if row is None:
                break
            nb = tuple_nbytes(schema, row)
            if build_bytes + nb > build_limit:
                self.page_reads += reader.page_reads
                return None
            rows.append(row)
            build_bytes += nb
        self.page_reads += reader.page_reads
        return rows

    def _bnl_partition(
        self,
        left_path,
        right_path,
        left_schema: list,
        right_schema: list,
        li: list[int],
        ri: list[int],
        block_capacity: int,
    ) -> None:
        """Memory-bounded BNL on one overflowing partition pair.

        Outer (left) is read in blocks of ~block_capacity bytes; for each block
        the right partition is scanned from disk (never held in full).
        """
        self.overflow_partitions += 1
        outer = TempRunReader(left_path, left_schema)
        block: list[list] = []
        block_bytes = 0

        def process_block(blk: list[list]) -> None:
            if not blk:
                return
            inner = TempRunReader(right_path, right_schema)
            while True:
                s = inner.next()
                if s is None:
                    break
                for r in blk:
                    self.comparisons += 1
                    if key_tuple(r, li) == key_tuple(s, ri):
                        self._out_buf.append(list(r) + list(s))
            self.page_reads += inner.page_reads

        while True:
            t = outer.next()
            if t is None:
                break
            nb = tuple_nbytes(left_schema, t)
            if block and block_bytes + nb > block_capacity:
                process_block(block)
                block = []
                block_bytes = 0
            block.append(t)
            block_bytes += nb
        process_block(block)
        self.page_reads += outer.page_reads

    def _probe_partition(
        self,
        build_rows: list[list],
        probe_path,
        right_schema: list,
        li: list[int],
        ri: list[int],
    ) -> None:
        """In-memory hash build on left partition, then probe right from disk."""
        table: dict[tuple, list[list]] = defaultdict(list)
        for r in build_rows:
            table[key_tuple(r, li)].append(r)

        reader = TempRunReader(probe_path, right_schema)
        while True:
            s = reader.next()
            if s is None:
                break
            self.comparisons += 1
            for r in table.get(key_tuple(s, ri), ()):
                self._out_buf.append(list(r) + list(s))
        self.page_reads += reader.page_reads

    def _join_partition(
        self,
        left_path,
        right_path,
        left_schema: list,
        right_schema: list,
        li: list[int],
        ri: list[int],
        build_limit: int,
        block_capacity: int,
    ) -> None:
        max_build_pages = max(1, build_limit // PAGE_SIZE)
        # Fast path: partition already larger than B-1 pages → BNL, no preload.
        if TempRunReader(left_path, left_schema).num_pages() > max_build_pages:
            self._bnl_partition(
                left_path, right_path, left_schema, right_schema,
                li, ri, block_capacity,
            )
            return

        build_rows = self._try_load_build(left_path, left_schema, build_limit)
        if build_rows is None:
            # Soft byte budget overflowed even though page count looked fine.
            self._bnl_partition(
                left_path, right_path, left_schema, right_schema,
                li, ri, block_capacity,
            )
        else:
            self._probe_partition(build_rows, right_path, right_schema, li, ri)

    def next(self):
        if self._out_i >= len(self._out_buf):
            return END
        row = self._out_buf[self._out_i]
        self._out_i += 1
        return self._emit(row)

    def close(self) -> None:
        self.left.close()
        self.right.close()
        self._closed = True
