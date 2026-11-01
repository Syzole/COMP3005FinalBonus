"""Heap scan pin discipline, delete-all, catalog round-trip."""

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from storage.catalog import Catalog
from storage.loader import load_csv, parse_schema


def test_create_load_scan():
    with tempfile.TemporaryDirectory() as tmp:
        cat = Catalog(tmp, frames=8)
        schema = parse_schema("a:INT,b:VARCHAR(5),c:INT")
        cat.create_table("R", schema)
        csv = Path(tmp) / "r.csv"
        csv.write_text("42,bob,7\n43,amy,9\n", encoding="utf-8")
        n = load_csv(cat, "R", csv, frames=8)
        assert n == 2
        heap, pool, info = cat.open_heap("R", frames=4)
        rows = [t for _, t in heap.scan()]
        assert rows == [[42, "bob", 7], [43, "amy", 9]]
        assert info["schema"][0] == ["a", "INT"]


def test_delete_all_scan_empty():
    with tempfile.TemporaryDirectory() as tmp:
        cat = Catalog(tmp, frames=8)
        cat.create_table("R", parse_schema("a:INT"))
        heap, pool, _ = cat.open_heap("R", frames=4)
        rids = []
        for i in range(10):
            rids.append(heap.insert([i]))
        for rid in rids:
            heap.delete(rid)
        pool.flush_all()
        rows = list(heap.scan())
        assert rows == []


def test_scan_pin_limit():
    with tempfile.TemporaryDirectory() as tmp:
        cat = Catalog(tmp, frames=8)
        cat.create_table("R", parse_schema("a:INT"))
        heap, pool, _ = cat.open_heap("R", frames=8)
        for i in range(500):
            heap.insert([i])
        pool.flush_all()

        max_pinned = 0
        # Re-open to clear pins
        heap, pool, _ = cat.open_heap("R", frames=8)
        gen = heap.scan()
        try:
            while True:
                next(gen)
                max_pinned = max(max_pinned, pool.pinned_count())
        except StopIteration:
            pass
        assert max_pinned <= 2, f"pinned {max_pinned} pages during scan"


def test_section_3_2_via_layout_numbers():
    # Already covered in test_page; keep a smoke create.
    with tempfile.TemporaryDirectory() as tmp:
        cat = Catalog(tmp)
        cat.create_table("T", parse_schema("x:INT,y:INT"))
        assert "T" in cat.list_tables()


if __name__ == "__main__":
    test_create_load_scan()
    test_delete_all_scan_empty()
    test_scan_pin_limit()
    test_section_3_2_via_layout_numbers()
    print("test_heap: all ok")
