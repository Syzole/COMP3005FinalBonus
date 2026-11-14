"""M1 §5.3 buffer pool worked example + pin errors."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from storage.disk import DiskManager
from storage.buffer import BufferPool, BufferError


def _pool(tmpdir, policy, B=3):
    path = Path(tmpdir) / "t.db"
    disk = DiskManager(path)
    disk.ensure_pages(10)
    return BufferPool(B, policy, disk)


def run_trace(policy):
    with tempfile.TemporaryDirectory() as tmp:
        pool = _pool(tmp, policy, B=3)
        for pid in [1, 2, 3, 1, 4, 2]:
            pool.fetch_page(pid)
            pool.unpin_page(pid, False)
        return pool.stats()


class TestBuffer(unittest.TestCase):
    def test_section_5_3_lru(self):
        s = run_trace("lru")
        self.assertEqual(s["hits"], 1, s)
        self.assertEqual(s["misses"], 5, s)
        self.assertEqual(s["evictions"], 2, s)

    def test_section_5_3_clock(self):
        s = run_trace("clock")
        self.assertEqual(s["hits"], 2, s)
        self.assertEqual(s["misses"], 4, s)
        self.assertEqual(s["evictions"], 1, s)

    def test_unpin_at_zero_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = _pool(tmp, "lru", B=2)
            pool.fetch_page(0)
            pool.unpin_page(0, False)
            with self.assertRaises(BufferError):
                pool.unpin_page(0, False)

    def test_pinned_eviction_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = _pool(tmp, "lru", B=2)
            pool.fetch_page(0)  # stay pinned
            pool.fetch_page(1)  # stay pinned
            with self.assertRaises(BufferError) as cm:
                pool.fetch_page(2)
            msg = str(cm.exception).lower()
            self.assertTrue("unpinned" in msg or "frame" in msg)

    def test_deterministic(self):
        a = run_trace("lru")
        b = run_trace("lru")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
