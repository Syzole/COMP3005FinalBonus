"""Heap scan pin discipline, delete-all, catalog round-trip, error paths."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from storage.catalog import Catalog
from storage.heap import HeapError
from storage.loader import load_csv, parse_schema, LoadError
from storage.page import dir_entry_count, dir_get_entry, dir_get_next, NO_NEXT


def _count_data_pages(heap) -> int:
    """Walk the directory chain and count registered data pages."""
    n = 0
    dir_id = 0
    while True:
        page = heap.pool.fetch_page(dir_id)
        count = dir_entry_count(page.data)
        n += count
        nxt = dir_get_next(page.data)
        heap.pool.unpin_page(dir_id, False)
        if nxt == NO_NEXT:
            break
        dir_id = nxt
    return n


class TestHeap(unittest.TestCase):
    def test_create_load_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=8)
            schema = parse_schema("a:INT,b:VARCHAR(5),c:INT")
            cat.create_table("R", schema)
            csv = Path(tmp) / "r.csv"
            csv.write_text("42,bob,7\n43,amy,9\n", encoding="utf-8")
            n = load_csv(cat, "R", csv, frames=8)
            self.assertEqual(n, 2)
            heap, pool, info = cat.open_heap("R", frames=4)
            rows = [t for _, t in heap.scan()]
            self.assertEqual(rows, [[42, "bob", 7], [43, "amy", 9]])
            self.assertEqual(info["schema"][0], ["a", "INT"])

    def test_delete_all_scan_empty(self):
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
            self.assertEqual(rows, [])

    def test_scan_pin_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=8)
            cat.create_table("R", parse_schema("a:INT"))
            heap, pool, _ = cat.open_heap("R", frames=8)
            for i in range(500):
                heap.insert([i])
            pool.flush_all()

            max_pinned = 0
            heap, pool, _ = cat.open_heap("R", frames=8)
            gen = heap.scan()
            try:
                while True:
                    next(gen)
                    max_pinned = max(max_pinned, pool.pinned_count())
            except StopIteration:
                pass
            self.assertLessEqual(max_pinned, 2, f"pinned {max_pinned} pages during scan")

    def test_scan_pin_limit_20x_b(self):
        """M1spec §8: scan a heap with ≥20×B data pages; ≤2 pages pinned."""
        B = 4
        min_data_pages = 20 * B  # 80
        # INT record = 4 bytes + 4-byte slot ≈ 511 tuples/page; pad for dir growth.
        n_rows = min_data_pages * 520
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=B)
            cat.create_table("Big", parse_schema("a:INT"))
            heap, pool, _ = cat.open_heap("Big", frames=B)
            for i in range(n_rows):
                heap.insert([i])
            pool.flush_all()

            heap, pool, _ = cat.open_heap("Big", frames=B)
            data_pages = _count_data_pages(heap)
            self.assertGreaterEqual(
                data_pages, min_data_pages, f"only {data_pages} data pages"
            )

            max_pinned = 0
            gen = heap.scan()
            try:
                while True:
                    next(gen)
                    max_pinned = max(max_pinned, pool.pinned_count())
            except StopIteration:
                pass
            self.assertLessEqual(
                max_pinned, 2, f"pinned {max_pinned} pages during large scan"
            )

    def test_oversize_record_names_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=8)
            # VARCHAR max is 255; many wide columns still exceed the 4088-byte page limit.
            schema = [[f"c{i}", "VARCHAR", 255] for i in range(17)]
            cat.create_table("Wide", schema)
            heap, pool, _ = cat.open_heap("Wide", frames=8)
            with self.assertRaises(HeapError) as cm:
                heap.insert(["x" * 255] * 17)
            self.assertIn("Wide", str(cm.exception))
            self.assertIn("4088", str(cm.exception))

    def test_bad_csv_names_line_and_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=8)
            cat.create_table("R", parse_schema("a:INT,b:VARCHAR(5)"))
            csv = Path(tmp) / "bad.csv"
            csv.write_text("1,ok\nnotint,x\n", encoding="utf-8")
            with self.assertRaises(LoadError) as cm:
                load_csv(cat, "R", csv, frames=8)
            msg = str(cm.exception)
            self.assertIn("line 2", msg)
            self.assertIn("column a", msg)

    def test_section_3_2_via_layout_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp)
            cat.create_table("T", parse_schema("x:INT,y:INT"))
            self.assertIn("T", cat.list_tables())


if __name__ == "__main__":
    unittest.main()
