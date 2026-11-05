"""Temporary run / partition files using fixed 4096-byte pages.

The purpose of this part is too handle when we dont hvae the space to hold all data in memory
i/o on these files is charged to that operator's page reads/writes
The cleanup function deletes the files when the operator is closed
"""

from __future__ import annotations

from pathlib import Path

from storage.disk import DiskManager
from storage.page import encode_tuple, decode_tuple, SlottedPage, NO_SPACE


class TempRunWriter:
    """
    Append-only: fill a slotted page, flush when full, allocate next page.
    close() flushes the last partial page and returns total page count
    """

    def __init__(self, path: Path, schema: list):
        self.path = path
        self.schema = schema
        self.disk = DiskManager(path)
        self.page_writes = 0
        self.page_reads = 0
        self.num_tuples = 0
        self._page: SlottedPage | None = None
        self._page_id: int | None = None

    def _ensure_page(self) -> SlottedPage:
        if self._page is None:
            pid = self.disk.allocate_page()
            self._page = Spotted(pid)
            self._page_id = pid
        return self._page

    def write(self, values: list) -> None:
        record = encode_tuple(self.schema, values)
        page = self._ensure_page()
        slot = page.insert(record)
        if slot == NO_SPACE:
            self._flush()
            page = self._ensure_page()
            slot = page.insert(record)
            if slot == NO_SPACE:
                raise RuntimeError("tuple larger than one page")
        self.num_tuples += 1

    def _flush(self) -> None:
        if self._page is None:
            return
        self.disk.write_page(self._page_id, self._page.data)
        self.page_writes += 1
        self._page = None
        self._page_id = None

    def close(self) -> int:
        self._flush()
        return self.disk.num_pages()

    def num_pages(self) -> int:
        return self.disk.num_pages()


def Spotted(page_id: int) -> SlottedPage:
    return SlottedPage(page_id=page_id)


class TempRunReader:
    """
    Sequential scan of a temp file, one page buffered at a time.
    reset() rewinds 
    """

    def __init__(self, path: Path, schema: list, disk: DiskManager | None = None):
        self.path = path
        self.schema = schema
        self.disk = disk if disk is not None else DiskManager(path)
        self.page_reads = 0
        self.page_writes = 0
        self._pid = 0
        self._buf: list[list] = []
        self._i = 0
        self._done = self.disk.num_pages() == 0

    def _load(self) -> bool:
        n = self.disk.num_pages()
        while self._pid < n:
            data = self.disk.read_page(self._pid)
            self.page_reads += 1
            sp = SlottedPage(data, page_id=self._pid)
            self._pid += 1
            rows = [decode_tuple(self.schema, sp.get(slot)) for slot in sp.live_slots()]
            if rows:
                self._buf = rows
                self._i = 0
                return True
        self._done = True
        return False

    def next(self) -> list | None:
        if self._done:
            return None
        if self._i >= len(self._buf):
            if not self._load():
                return None
        row = self._buf[self._i]
        self._i += 1
        return row

    def reset(self) -> None:
        self._pid = 0
        self._buf = []
        self._i = 0
        self._done = self.disk.num_pages() == 0

    def num_pages(self) -> int:
        return self.disk.num_pages()


def drain_to_temp(child, schema: list, path: Path) -> TempRunWriter:# Pull every tuple from an already-open child into a temp file
    """Drain an open child iterator into a temp run and close the writer."""
    from exec.iterator import END

    w = TempRunWriter(path, schema)
    while True:
        t = child.next()
        if t is END:
            break
        w.write(t)
    w.close()
    return w
