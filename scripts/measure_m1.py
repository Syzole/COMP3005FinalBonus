"""Produce REPORT_M1 measurement numbers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.disk import DiskManager
from storage.buffer import BufferPool
from storage.page import SlottedPage, encode_tuple
from storage.catalog import Catalog
from storage.loader import parse_schema


def capacity_measurement():
    schemas = {
        "all_int": parse_schema("a:INT,b:INT,c:INT"),  # L=12
        "short_varchar": parse_schema("a:INT,b:VARCHAR(5),c:INT"),  # L=13 for 'bob'
        "long_varchar": parse_schema("a:INT,b:VARCHAR(200)"),
    }
    rows = []
    for label, schema in schemas.items():
        # Hand formula for fixed L when possible
        if label == "all_int":
            L = 12
            computed = 4088 // (L + 4)
            sample = [1, 2, 3]
        elif label == "short_varchar":
            L = 4 + (2 + 3) + 4  # INT + 'bob' + INT = 13
            computed = 4088 // (L + 4)
            sample = [42, "bob", 7]
        else:
            # measure with VARCHAR of length 100 (UTF-8 ascii)
            s = "x" * 100
            L = 4 + (2 + 100)
            computed = 4088 // (L + 4)
            sample = [1, s]

        page = SlottedPage(page_id=0)
        measured = 0
        rec = encode_tuple(schema, sample)
        while page.insert(rec) != -1:
            measured += 1
        rows.append((label, L, computed, measured, len(rec)))
    return rows


def run_trace_file(path: Path, policy: str, B: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        disk = DiskManager(Path(tmp) / "t.db")
        ids = [int(line) for line in path.read_text().splitlines() if line.strip()]
        if ids:
            disk.ensure_pages(max(ids) + 1)
        pool = BufferPool(B, policy, disk)
        for pid in ids:
            pool.fetch_page(pid)
            pool.unpin_page(pid, False)
        s = pool.stats()
        total = s["hits"] + s["misses"]
        hit_rate = s["hits"] / total if total else 0.0
        return {**s, "hit_rate": hit_rate, "refs": total}


def policy_table():
    traces = sorted((ROOT / "traces").glob("trace*.txt"))
    frames = [4, 16, 64, 256]
    table = []
    for tr in traces:
        for policy in ("lru", "clock"):
            for B in frames:
                s = run_trace_file(tr, policy, B)
                table.append({
                    "trace": tr.name,
                    "policy": policy,
                    "B": B,
                    "hit_rate": round(s["hit_rate"], 4),
                    "hits": s["hits"],
                    "misses": s["misses"],
                    "evictions": s["evictions"],
                })
    return table


def scan_cost():
    """Scan a ~500 page table at B=2,8,64."""
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        cat = Catalog(tmp, frames=64)
        # one INT column; ~240 records/page of data + directory → aim for 500 data pages
        schema = parse_schema("a:INT")
        cat.create_table("Big", schema)
        heap, pool, _ = cat.open_heap("Big", frames=64)
        # fill until disk has >= 501 pages (page 0 is directory)
        target_pages = 501
        i = 0
        while pool.disk.num_pages() < target_pages:
            heap.insert([i])
            i += 1
        pool.flush_all()
        data_pages = pool.disk.num_pages() - 1
        cat.update_table_stats("Big", pool.disk.num_pages(), i)

        for B in (2, 8, 64):
            heap, pool, _ = cat.open_heap("Big", frames=B)
            # reset disk counters by reading stats after scan; DiskManager counters
            # start at 0 on new DiskManager inside open_heap
            n = 0
            for _ in heap.scan():
                n += 1
            s = pool.stats()
            results.append({
                "B": B,
                "tuples": n,
                "file_pages": pool.disk.num_pages(),
                "data_pages_approx": data_pages,
                "reads": s["reads"],
                "writes": s["writes"],
            })
    return results

def main():
    cap = capacity_measurement()
    pols = policy_table()
    scans = scan_cost()

    out = {
        "capacity": [
            {"schema": a, "L": L, "computed": c, "measured": m, "record_bytes": rb}
            for a, L, c, m, rb in cap
        ],
        "policies": pols,
        "scan": scans,
    }
    dest = ROOT / "data" / "m1_measurements.json"
    #make sure the directory exists
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("wrote", dest)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
