"""Table statistics: JSON sidecar files (catalog records cap at 4088 bytes)."""

from __future__ import annotations

import json
from pathlib import Path

MARKER = b"STATS_FILE_V1"  # catalog blob points at sidecar


def stats_path(db_dir: Path | str, table: str) -> Path:
    return Path(db_dir) / f"{table}.stats.json"


def pack_stats(obj: dict, db_dir: Path | str | None = None, table: str | None = None) -> bytes:
    """Write full JSON to sidecar when db_dir/table given; catalog gets a short marker."""
    if db_dir is not None and table is not None:
        path = stats_path(db_dir, table)
        path.write_text(
            json.dumps(obj, sort_keys=True, indent=2), encoding="utf-8"
        )
        return MARKER  # store marker, not full JSON
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > 3500:
        raise ValueError("stats blob too large; pass db_dir/table for sidecar storage")
    return raw  # inline blob for tiny stats


def unpack_stats(blob: bytes, db_dir: Path | str | None = None, table: str | None = None) -> dict | None:
    if not blob:
        return None
    if blob == MARKER:
        if db_dir is None or table is None:
            return None
        path = stats_path(db_dir, table)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))  # load sidecar
    return json.loads(blob.decode("utf-8"))


def empty_defaults(n_tuples: int = 1000, n_pages: int = 10) -> dict:
    """Defaults when ANALYZE has not been run."""
    return {
        "n_tuples": n_tuples,
        "n_pages": n_pages,
        "width": 32.0,
        "columns": {},
    }


def col_stats(stats: dict | None, col: str) -> dict:
    """col may be qualified; look up bare or qualified."""
    if not stats:
        return {"ndv": max(1, 100), "min": None, "max": None, "hist": []}
    cols = stats.get("columns", {})
    bare = col.split(".")[-1]  # e.age -> age
    if col in cols:
        return cols[col]
    if bare in cols:
        return cols[bare]
    # match any *.bare key
    for k, v in cols.items():
        if k.split(".")[-1] == bare:
            return v
    # rough default: ~10% distinct
    return {"ndv": max(1, stats.get("n_tuples", 1000) // 10), "min": None, "max": None, "hist": []}
