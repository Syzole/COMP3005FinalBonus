"""M1 confidence checks beyond the worked examples.

Record layout, quoted CSV, slot identity, buffer counters, and a scan
whose read count does not fall when the frame budget grows.
"""

from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from storage.buffer import BufferPool
from storage.catalog import Catalog
from storage.disk import DiskManager
from storage.heap import HeapError
from storage.loader import LoadError, load_csv, parse_schema
from storage.page import SlottedPage, decode_tuple, encode_tuple


class TestRecordBytes(unittest.TestCase):
    def test_section_2_example_is_13_bytes(self):
        schema = [["a", "INT"], ["b", "VARCHAR", 5], ["c", "INT"]]
        raw = encode_tuple(schema, [42, "bob", 7])
        self.assertEqual(len(raw), 13)
        self.assertEqual(raw[:4], struct.pack("<i", 42))
        self.assertEqual(raw[4:6], struct.pack("<H", 3))
        self.assertEqual(raw[6:9], b"bob")
        self.assertEqual(raw[9:13], struct.pack("<i", 7))
        self.assertEqual(decode_tuple(schema, raw), [42, "bob", 7])

    def test_utf8_roundtrip(self):
        schema = [["name", "VARCHAR", 20]]
        raw = encode_tuple(schema, ["café"])
        # c,a,f plus é (two UTF-8 bytes), plus the 2-byte length prefix.
        self.assertEqual(len(raw), 7)
        self.assertEqual(raw[:2], struct.pack("<H", 5))
        self.assertEqual(decode_tuple(schema, raw), ["café"])

    def test_varchar_over_255_rejected(self):
        with self.assertRaises(LoadError) as cm:
            parse_schema("a:VARCHAR(256)")
        self.assertIn("255", str(cm.exception))


class TestCsv(unittest.TestCase):
    def test_quoted_comma_is_one_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=8)
            cat.create_table("R", parse_schema("name:VARCHAR(20),n:INT"))
            csv = Path(tmp) / "r.csv"
            csv.write_text('"bob, jr",7\n', encoding="utf-8")
            n = load_csv(cat, "R", csv, frames=8)
            self.assertEqual(n, 1)
            heap, _pool, _info = cat.open_heap("R", frames=4)
            rows = [t for _, t in heap.scan()]
            self.assertEqual(rows, [["bob, jr", 7]])

    def test_varchar_too_long_names_line_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=8)
            cat.create_table("R", parse_schema("a:VARCHAR(3),b:INT"))
            csv = Path(tmp) / "bad.csv"
            csv.write_text("ab,1\nabcd,2\nzz,3\n", encoding="utf-8")
            with self.assertRaises(LoadError) as cm:
                load_csv(cat, "R", csv, frames=8)
            msg = str(cm.exception)
            self.assertIn("line 2", msg)
            self.assertIn("column a", msg)
            heap, _pool, _info = cat.open_heap("R", frames=4)
            values = [t[0] for _, t in heap.scan()]
            self.assertNotIn("zz", values)


class TestSlots(unittest.TestCase):
    def test_slot_not_reused_and_dead_rid_names_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=8)
            cat.create_table("R", parse_schema("a:INT"))
            heap, pool, _info = cat.open_heap("R", frames=4)
            first = heap.insert([1])
            second_keep = heap.insert([2])
            page = pool.fetch_page(first[0])
            free_end = SlottedPage(page.data, page_id=first[0]).free_end
            pool.unpin_page(first[0], False)
            heap.delete(first)
            page = pool.fetch_page(first[0])
            after = SlottedPage(page.data, page_id=first[0]).free_end
            pool.unpin_page(first[0], False)
            self.assertEqual(after, free_end)
            fresh = heap.insert([3])
            self.assertNotEqual(fresh[1], first[1])
            with self.assertRaises(HeapError) as cm:
                heap.get(first)
            self.assertIn(str(first), str(cm.exception))
            self.assertEqual(heap.num_tuples(), 2)
            self.assertEqual(second_keep[1], 1)


class TestBufferCounters(unittest.TestCase):
    def test_hit_does_not_read_and_first_misses_are_not_evictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = DiskManager(Path(tmp) / "t.db")
            disk.ensure_pages(6)
            pool = BufferPool(3, "lru", disk)
            for pid in (0, 1, 2):
                pool.fetch_page(pid)
                pool.unpin_page(pid, False)
            stats = pool.stats()
            self.assertEqual(stats["misses"], 3)
            self.assertEqual(stats["evictions"], 0)
            self.assertEqual(stats["hits"], 0)
            reads = stats["reads"]
            pool.fetch_page(0)
            after = pool.stats()
            self.assertEqual(after["hits"], 1)
            self.assertEqual(after["reads"], reads)
            self.assertEqual(after["misses"], 3)

    def test_dirty_eviction_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = DiskManager(Path(tmp) / "t.db")
            disk.ensure_pages(4)
            pool = BufferPool(2, "lru", disk)
            pool.fetch_page(0)
            pool.unpin_page(0, True)
            pool.fetch_page(1)
            pool.unpin_page(1, False)
            self.assertEqual(pool.stats()["writes"], 0)
            pool.fetch_page(2)
            self.assertGreaterEqual(pool.stats()["writes"], 1)
            self.assertEqual(pool.stats()["evictions"], 1)


class TestScanReads(unittest.TestCase):
    def test_scan_reads_do_not_fall_as_b_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = Catalog(tmp, frames=8)
            cat.create_table("R", parse_schema("a:INT,b:VARCHAR(200)"))
            heap, pool, _info = cat.open_heap("R", frames=8)
            for i in range(80):
                heap.insert([i, "x" * 200])
            pool.flush_all()

            def scan_reads(frames: int) -> int:
                h, p, _ = cat.open_heap("R", frames=frames)
                before = p.stats()["reads"]
                list(h.scan())
                return p.stats()["reads"] - before

            small = scan_reads(2)
            large = scan_reads(8)
            self.assertGreater(small, 2)
            self.assertEqual(small, large)


if __name__ == "__main__":
    unittest.main()
