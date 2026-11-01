"""Disk manager: fixed 4096-byte pages, counted reads/writes."""

from __future__ import annotations

from pathlib import Path

PAGE_SIZE = 4096


class DiskError(Exception):
    pass


class DiskManager:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_bytes(b"")
        self.reads = 0
        self.writes = 0

    def num_pages(self) -> int:
        return self.path.stat().st_size // PAGE_SIZE

    def allocate_page(self) -> int:
        page_id = self.num_pages()
        with self.path.open("ab") as f:
            f.write(bytes(PAGE_SIZE))
        return page_id

    def ensure_pages(self, count: int) -> None:
        """Grow the file so it has at least `count` pages (zero-filled)."""
        while self.num_pages() < count:
            self.allocate_page()

    def read_page(self, page_id: int) -> bytearray:
        if page_id < 0 or page_id >= self.num_pages():
            raise DiskError(f"page_id {page_id} out of range (num_pages={self.num_pages()})")
        with self.path.open("rb") as f:
            f.seek(page_id * PAGE_SIZE)
            data = f.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise DiskError(f"short read on page {page_id}")
        self.reads += 1
        return bytearray(data)

    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        if len(data) != PAGE_SIZE:
            raise DiskError(f"page must be {PAGE_SIZE} bytes, got {len(data)}")
        if page_id < 0 or page_id >= self.num_pages():
            raise DiskError(f"page_id {page_id} out of range (num_pages={self.num_pages()})")
        with self.path.open("r+b") as f:
            f.seek(page_id * PAGE_SIZE)
            f.write(data)
        self.writes += 1
