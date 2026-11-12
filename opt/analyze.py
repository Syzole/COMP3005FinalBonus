"""ANALYZE: exact table statistics via heap scan + column sort for hist."""

from __future__ import annotations

from storage.catalog import Catalog
from storage.page import encode_tuple
from opt.stats import pack_stats


def analyze_table(catalog: Catalog, table: str, buckets: int = 20, frames: int = 32) -> dict:
    info = catalog.get_table(table)
    if info is None:
        raise ValueError(f"unknown table {table!r}")

    heap, pool, _ = catalog.open_heap(table, frames=max(4, frames), policy="lru")
    schema = info["schema"]
    ncols = len(schema)
    col_values: list[list] = [[] for _ in range(ncols)]  # one list per column
    n_tuples = 0
    total_bytes = 0
    data_pages = set()

    # single heap scan — collect all column values
    for rid, tup in heap.scan():
        n_tuples += 1
        data_pages.add(rid[0])  # track live data pages
        total_bytes += len(encode_tuple(schema, tup))
        for i, v in enumerate(tup):
            col_values[i].append(v)

    pool.flush_all()
    n_pages = max(1, len(data_pages)) if n_tuples else 0
    width = (total_bytes / n_tuples) if n_tuples else 0.0  # avg record bytes

    columns = {}
    for i, col in enumerate(schema):
        name = col[0]
        vals = col_values[i]
        if not vals:
            columns[name] = {"ndv": 0, "min": None, "max": None, "hist": []}
            continue
        # one-run sort (stdlib OK when it fits)
        sorted_vals = sorted(vals)
        ndv = len(set(sorted_vals))
        vmin, vmax = sorted_vals[0], sorted_vals[-1]
        hist = _equi_depth(sorted_vals, buckets)
        columns[name] = {
            "ndv": ndv,
            "min": _jsonable(vmin),
            "max": _jsonable(vmax),
            "hist": hist,
        }

    stats = {
        "n_tuples": n_tuples,
        "n_pages": n_pages,
        "width": width,
        "columns": columns,
        "buckets": buckets,
    }
    # write sidecar + catalog marker
    catalog.update_table_stats(
        table, n_pages, n_tuples, pack_stats(stats, catalog.db_dir, table)
    )
    return stats


def _jsonable(v):
    """Make value JSON-safe."""
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)


def _equi_depth(sorted_vals: list, k: int) -> list[dict]:
    """Cut sorted column into k equal-count buckets."""
    n = len(sorted_vals)
    if n == 0 or k <= 0:
        return []
    k = min(k, n)
    hist = []
    for b in range(k):
        start = (b * n) // k
        end = ((b + 1) * n) // k
        chunk = sorted_vals[start:end]
        if not chunk:
            continue
        hist.append(
            {
                "lo": _jsonable(chunk[0]),
                "hi": _jsonable(chunk[-1]),
                "count": len(chunk),
                "ndv": len(set(chunk)),
            }
        )
    return hist
