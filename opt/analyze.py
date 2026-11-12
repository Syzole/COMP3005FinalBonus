"""ANALYZE: exact table statistics via heap scan + M2 external sort for hist.

M4spec §1: equi-depth histograms are built by sorting each column and cutting
every n/k-th value. The sort must use Module 2's external sort so a large
table never holds a full column in memory for ordering.
"""

from __future__ import annotations

from storage.catalog import Catalog
from storage.page import encode_tuple, PAGE_SIZE, SlottedPage, NO_SPACE
from opt.stats import pack_stats


class _IoCounter:
    """Minimal stand-in for Operator I/O fields used by RunFile/RunSource."""

    def __init__(self):
        self.page_reads = 0
        self.page_writes = 0


def analyze_table(catalog: Catalog, table: str, buckets: int = 20, frames: int = 32) -> dict:
    info = catalog.get_table(table)
    if info is None:
        raise ValueError(f"unknown table {table!r}")

    frames = max(4, int(frames))
    heap, pool, _ = catalog.open_heap(table, frames=frames, policy="lru")
    schema = info["schema"]
    ncols = len(schema)
    n_tuples = 0
    total_bytes = 0
    data_pages: set[int] = set()

    # Spill each column to a temp run during the single heap scan (disk-backed).
    from exec.context import Context

    ctx = Context(catalog.db_dir, frames)
    io = _IoCounter()
    try:
        col_schemas = [_col_schema(i, col) for i, col in enumerate(schema)]
        writers = [_RunWriter(ctx, sch, io) for sch in col_schemas]

        for rid, tup in heap.scan():
            n_tuples += 1
            data_pages.add(rid[0])
            total_bytes += len(encode_tuple(schema, tup))
            for i, v in enumerate(tup):
                writers[i].append(v)

        for w in writers:
            w.finish()
        pool.flush_all()

        n_pages = max(1, len(data_pages)) if n_tuples else 0
        width = (total_bytes / n_tuples) if n_tuples else 0.0

        columns = {}
        for i, col in enumerate(schema):
            name = col[0]
            run = writers[i].run
            if n_tuples == 0 or run is None or run.num_pages() == 0:
                columns[name] = {"ndv": 0, "min": None, "max": None, "hist": []}
                continue
            summary = _sort_column_m2(
                ctx, col_schemas[i], run, buckets, n_tuples, frames
            )
            columns[name] = summary

        stats = {
            "n_tuples": n_tuples,
            "n_pages": n_pages,
            "width": width,
            "columns": columns,
            "buckets": buckets,
        }
        catalog.update_table_stats(
            table, n_pages, n_tuples, pack_stats(stats, catalog.db_dir, table)
        )
        return stats
    finally:
        ctx.cleanup()


def _col_schema(index: int, col: list) -> list:
    """Qualified one-column schema for M2 Sort keys."""
    qname = f"__anal{index}.v"
    if col[1] == "INT":
        return [[qname, "INT"]]
    return [[qname, "VARCHAR", col[2]]]


class _RunWriter:
    """Append single-column tuples into a RunFile one slotted page at a time."""

    def __init__(self, ctx, schema: list, io: _IoCounter):
        self.ctx = ctx
        self.schema = schema
        self.io = io
        self.run = None
        self._slot = None
        self._sp = None

    def append(self, value) -> None:
        from storage.page import encode_tuple as enc

        if self.run is None:
            self.run = self.ctx.new_run()
            self._slot = self.ctx.slot()
            self._sp = SlottedPage.fresh(self._slot.buf, 0)
        rec = enc(self.schema, [value])
        if len(rec) + 4 > PAGE_SIZE - 8:
            raise ValueError("ANALYZE value does not fit in a page")
        if self._sp.insert(rec) == NO_SPACE:
            self.run.write_page(self._slot.buf, self.io)
            self._sp = SlottedPage.fresh(self._slot.buf, 0)
            if self._sp.insert(rec) == NO_SPACE:
                raise ValueError("ANALYZE value does not fit in a page")

    def finish(self) -> None:
        if self.run is None:
            self.run = self.ctx.new_run()
            return
        if self._sp is not None and self._sp.slot_count > 0:
            self.run.write_page(self._slot.buf, self.io)
        if self._slot is not None:
            self._slot.release()
            self._slot = None
            self._sp = None


def _sort_column_m2(ctx, schema: list, run, buckets: int, n_tuples: int, frames: int) -> dict:
    """External-sort one spilled column with M2 Sort; stream for hist/ndv/min/max."""
    from exec.ops import Sort, RunSource, END

    io = _IoCounter()
    src = RunSource(ctx, run, schema, io)
    key = schema[0][0]
    sorter = Sort(ctx, src, keys=[(key, "asc")], spill_final=False)
    sorter.prepare()
    sorter.open()
    try:
        return _summarize_sorted_stream(sorter, buckets, n_tuples)
    finally:
        sorter.close()


def _summarize_sorted_stream(sorter, buckets: int, n_tuples: int) -> dict:
    """One pass over sorted next(): ndv via runs, equi-depth via index cuts."""
    k = min(buckets, n_tuples) if n_tuples else 0
    # bucket i covers indices [bounds[i], bounds[i+1])
    bounds = [(b * n_tuples) // k for b in range(k + 1)] if k else [0]

    hist: list[dict] = []
    ndv = 0
    vmin = vmax = None
    prev = object()
    idx = 0
    bucket_i = 0
    bucket_lo = None
    bucket_hi = None
    bucket_count = 0
    bucket_ndv = 0
    bucket_prev = object()

    def close_bucket():
        nonlocal bucket_i, bucket_lo, bucket_hi, bucket_count, bucket_ndv, bucket_prev
        if bucket_count == 0:
            return
        hist.append(
            {
                "lo": _jsonable(bucket_lo),
                "hi": _jsonable(bucket_hi),
                "count": bucket_count,
                "ndv": bucket_ndv,
            }
        )
        bucket_i += 1
        bucket_lo = bucket_hi = None
        bucket_count = 0
        bucket_ndv = 0
        bucket_prev = object()

    from exec.ops import END

    while True:
        tup = sorter.next()
        if tup is END:
            break
        v = tup[0]
        if idx == 0:
            vmin = v
        vmax = v
        if v != prev:
            ndv += 1
            prev = v

        # advance bucket if this index starts a new cut
        while bucket_i + 1 < len(bounds) - 1 and idx >= bounds[bucket_i + 1]:
            close_bucket()

        if bucket_lo is None:
            bucket_lo = v
        bucket_hi = v
        bucket_count += 1
        if v != bucket_prev:
            bucket_ndv += 1
            bucket_prev = v
        idx += 1

    close_bucket()
    # If cuts left an empty trailing bucket range, ignore it.
    return {
        "ndv": ndv,
        "min": _jsonable(vmin),
        "max": _jsonable(vmax),
        "hist": hist,
    }


def _jsonable(v):
    """Make value JSON-safe."""
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)
