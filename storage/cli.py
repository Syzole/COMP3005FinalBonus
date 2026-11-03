"""Module 1 command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from storage.catalog import Catalog, CatalogError
from storage.loader import parse_schema, load_csv, LoadError
from storage.disk import DiskManager
from storage.buffer import BufferPool, BufferError
from storage.page import (
    SlottedPage, dir_entry_count, dir_get_entry, dir_get_next, NO_NEXT,
)


def cmd_create_table(args):
    schema = parse_schema(args.schema)
    cat = Catalog(args.db, frames=8, policy="lru")
    try:
        cat.create_table(args.name, schema)
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"created table {args.name}")
    return 0


def cmd_load(args):
    cat = Catalog(args.db, frames=args.frames, policy="lru")
    try:
        n = load_csv(cat, args.table, args.file, frames=args.frames)
    except (CatalogError, LoadError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"loaded {n} rows into {args.table}")
    return 0


def cmd_scan(args):
    cat = Catalog(args.db, frames=args.frames, policy="lru")
    try:
        heap, pool, info = cat.open_heap(args.table, frames=args.frames, policy="lru")
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    rows = []
    for _rid, tup in heap.scan():
        rows.append(tup)
        if args.limit is not None and len(rows) >= args.limit:
            break
    pool.flush_all()
    stats = pool.stats()
    if args.json:
        out = {
            "table": args.table,
            "schema": info["schema"],
            "rows": rows,
            "stats": stats,
        }
        print(json.dumps(out))
    else:
        for r in rows:
            print(r)
        print(stats)
    return 0


def _directory_page_ids(disk: DiskManager) -> set[int]:
    """Walk the directory chain starting at page 0."""
    ids = set()
    pid = 0
    while True:
        ids.add(pid)
        data = disk.read_page(pid)
        nxt = dir_get_next(data)
        if nxt == NO_NEXT:
            break
        pid = nxt
    return ids


def cmd_layout(args):
    cat = Catalog(args.db, frames=8, policy="lru")
    info = cat.get_table(args.table)
    if info is None:
        print(f"error: unknown table {args.table!r}", file=sys.stderr)
        return 1
    path = Path(args.db) / info["file_name"]
    disk = DiskManager(path)
    if args.page < 0 or args.page >= disk.num_pages():
        print(f"error: page {args.page} out of range", file=sys.stderr)
        return 1
    dir_ids = _directory_page_ids(disk)
    data = disk.read_page(args.page)
    if args.page in dir_ids:
        print(f"directory page {args.page}")
        print(f"  next_dir_page={dir_get_next(data)}")
        count = dir_entry_count(data)
        print(f"  entry_count={count}")
        for i in range(count):
            pid, free = dir_get_entry(data, i)
            print(f"  entry {i}: data_page_id={pid} free_bytes={free}")
    else:
        sp = SlottedPage(bytearray(data), page_id=args.page)
        for line in sp.layout_lines():
            print(line)
    return 0



def cmd_trace(args):
    db = Path(args.db)
    db.mkdir(parents=True, exist_ok=True)
    # Backing file for abstract page ids in the trace.
    path = db / "_trace.db"
    if path.exists():
        path.unlink()
    disk = DiskManager(path)
    page_ids = []
    with open(args.file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            page_ids.append(int(line))
    if page_ids:
        disk.ensure_pages(max(page_ids) + 1)

    pool = BufferPool(args.frames, args.policy, disk)
    try:
        for pid in page_ids:
            pool.fetch_page(pid)
            pool.unpin_page(pid, False)
    except BufferError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    stats = pool.stats()
    out = {
        "policy": args.policy.lower(),
        "frames": args.frames,
        "hits": stats["hits"],
        "misses": stats["misses"],
        "evictions": stats["evictions"],
        "reads": stats["reads"],
        "writes": stats["writes"],
    }
    if args.json:
        print(json.dumps(out))
    else:
        print(out)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="storage")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create-table")
    c.add_argument("--db", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--schema", required=True)
    c.set_defaults(func=cmd_create_table)

    c = sub.add_parser("load")
    c.add_argument("--db", required=True)
    c.add_argument("--table", required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--frames", type=int, default=32)
    c.set_defaults(func=cmd_load)

    c = sub.add_parser("scan")
    c.add_argument("--db", required=True)
    c.add_argument("--table", required=True)
    c.add_argument("--frames", type=int, default=8)
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_scan)

    c = sub.add_parser("layout")
    c.add_argument("--db", required=True)
    c.add_argument("--table", required=True)
    c.add_argument("--page", type=int, required=True)
    c.set_defaults(func=cmd_layout)

    c = sub.add_parser("trace")
    c.add_argument("--db", required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--frames", type=int, required=True)
    c.add_argument("--policy", choices=["lru", "clock", "LRU", "CLOCK"], required=True)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_trace)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
