"""M1 §5.3 buffer pool worked example + pin errors."""

from pathlib import Path
import sys
import tempfile

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


def test_section_5_3_lru():
    s = run_trace("lru")
    assert s["hits"] == 1, s
    assert s["misses"] == 5, s
    assert s["evictions"] == 2, s


def test_section_5_3_clock():
    s = run_trace("clock")
    assert s["hits"] == 2, s
    assert s["misses"] == 4, s
    assert s["evictions"] == 1, s


def test_unpin_at_zero_fails():
    with tempfile.TemporaryDirectory() as tmp:
        pool = _pool(tmp, "lru", B=2)
        pool.fetch_page(0)
        pool.unpin_page(0, False)
        try:
            pool.unpin_page(0, False)
            assert False, "expected error"
        except BufferError:
            pass


def test_pinned_eviction_fails():
    with tempfile.TemporaryDirectory() as tmp:
        pool = _pool(tmp, "lru", B=2)
        pool.fetch_page(0)  # stay pinned
        pool.fetch_page(1)  # stay pinned
        try:
            pool.fetch_page(2)
            assert False, "expected no unpinned frame"
        except BufferError as e:
            assert "unpinned" in str(e).lower() or "frame" in str(e).lower()


def test_deterministic():
    a = run_trace("lru")
    b = run_trace("lru")
    assert a == b


if __name__ == "__main__":
    test_section_5_3_lru()
    test_section_5_3_clock()
    test_unpin_at_zero_fails()
    test_pinned_eviction_fails()
    test_deterministic()
    print("test_buffer: all ok")
