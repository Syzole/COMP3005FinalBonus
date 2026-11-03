"""Catalog stored in catalog.db through the buffer pool (M1 §6).

On-disk format (page 0 header + variable table records across slotted pages
owned by a mini heap inside catalog.db):

We store one binary record per table in a heap file on catalog.db using the
same directory + slotted data pages as table heaps.

Record layout (little-endian):
  u16 name_len + name UTF-8
  u16 ncol
  for each column:
    u16 name_len + name UTF-8
    u8  type_tag   (0=INT, 1=VARCHAR)
    u8  vmax       (0 for INT, n for VARCHAR(n))
  u16 file_len + file_name UTF-8
  u32 num_pages
  u32 num_tuples
  u16 stats_len + stats blob   (0 for now; M4 will store ANALYZE payload)

Page 0 of catalog.db is the heap directory page.
"""

from __future__ import annotations

import struct
from pathlib import Path

from storage.disk import DiskManager
from storage.buffer import BufferPool
from storage.heap import HeapFile, HeapError


class CatalogError(Exception):
    pass


def _pack_table(
    name: str,
    schema: list,
    file_name: str,
    num_pages: int,
    num_tuples: int,
    stats: bytes = b"",
) -> bytes:
    # manual binary packing to create a single record
    parts = [struct.pack("<H", len(name.encode("utf-8"))), name.encode("utf-8")]
    parts.append(struct.pack("<H", len(schema)))
    for col in schema:
        cname = col[0].encode("utf-8")
        parts.append(struct.pack("<H", len(cname)))
        parts.append(cname)
        if col[1] == "INT":
            parts.append(struct.pack("<BB", 0, 0))
        elif col[1] == "VARCHAR":
            parts.append(struct.pack("<BB", 1, col[2]))
        else:
            raise CatalogError(f"unknown type {col[1]}")
    fb = file_name.encode("utf-8")
    parts.append(struct.pack("<H", len(fb)))
    parts.append(fb)
    parts.append(struct.pack("<II", num_pages, num_tuples))
    parts.append(struct.pack("<H", len(stats)))
    parts.append(stats)
    return b"".join(parts)


def _unpack_table(data: bytes) -> dict:
    # inverse of _pack_table — walk the same offsets
    off = 0
    (nlen,) = struct.unpack_from("<H", data, off)
    off += 2
    name = data[off:off + nlen].decode("utf-8")
    off += nlen
    (ncol,) = struct.unpack_from("<H", data, off)
    off += 2
    schema = []
    for _ in range(ncol):
        (clen,) = struct.unpack_from("<H", data, off)
        off += 2
        cname = data[off:off + clen].decode("utf-8")
        off += clen
        tag, vmax = struct.unpack_from("<BB", data, off)
        off += 2
        if tag == 0:
            schema.append([cname, "INT"])
        else:
            schema.append([cname, "VARCHAR", vmax])
    (flen,) = struct.unpack_from("<H", data, off)
    off += 2
    file_name = data[off:off + flen].decode("utf-8")
    off += flen
    num_pages, num_tuples = struct.unpack_from("<II", data, off)
    off += 8
    (slen,) = struct.unpack_from("<H", data, off)
    off += 2
    stats = data[off:off + slen]
    return {
        "name": name,
        "schema": schema,
        "file_name": file_name,
        "num_pages": num_pages,
        "num_tuples": num_tuples,
        "stats": stats,
        "first_page": 0,
    }


# catalog rows aren't normal tuples — we stash the packed blob as-is.
# HeapFile.insert wants schema values, so this subclass inserts raw bytes instead.


class _RawHeap(HeapFile):
    """Same insert/scan idea as HeapFile, but records are opaque bytes."""

    def insert_raw(self, record: bytes) -> tuple[int, int]: #insert raw bytes into the catalog
        from storage.page import (
            SlottedPage, NO_SPACE, init_directory_page, init_data_page,
            dir_get_next, dir_set_next, dir_entry_count, dir_set_entry_count,
            dir_get_entry, dir_set_entry, DIR_CAPACITY, NO_NEXT,
        )
        if len(record) > 4088:
            raise HeapError("catalog record too large")
        need = len(record) + 4
        dir_id = 0
        while True:
            dir_page = self.pool.fetch_page(dir_id)
            count = dir_entry_count(dir_page.data)
            for i in range(count):
                data_pid, free_bytes = dir_get_entry(dir_page.data, i)
                if free_bytes >= need:
                    self.pool.unpin_page(dir_id, False)
                    return self._insert_raw_into(data_pid, dir_id, i, record)
            next_id = dir_get_next(dir_page.data)
            if next_id != NO_NEXT:
                self.pool.unpin_page(dir_id, False)
                dir_id = next_id
                continue
            if count >= DIR_CAPACITY:
                new_dir = self.pool.new_page()
                init_directory_page(new_dir.data, new_dir.page_id)
                new_dir_id = new_dir.page_id
                dir_set_next(dir_page.data, new_dir_id)
                self.pool.unpin_page(dir_id, True)
                self.pool.unpin_page(new_dir_id, True)
                dir_id = new_dir_id
                continue
            data_page = self.pool.new_page()
            init_data_page(data_page.data, data_page.page_id)
            data_pid = data_page.page_id
            free = SpFree(data_page.data, data_pid)
            dir_set_entry(dir_page.data, count, data_pid, free)
            dir_set_entry_count(dir_page.data, count + 1)
            self.pool.unpin_page(data_pid, True)
            self.pool.unpin_page(dir_id, True)
            return self._insert_raw_into(data_pid, dir_id, count, record)

    def _insert_raw_into(self, data_pid, dir_id, entry_idx, record): # insert into a data page and update the directory
        from storage.page import SlottedPage, NO_SPACE, dir_set_entry
        page = self.pool.fetch_page(data_pid)
        sp = SlottedPage(page.data, page_id=data_pid)
        slot = sp.insert(record)
        if slot == NO_SPACE:
            self.pool.unpin_page(data_pid, False)
            raise HeapError("no space")
        free = sp.free_space()
        self.pool.unpin_page(data_pid, True)
        dir_page = self.pool.fetch_page(dir_id)
        dir_set_entry(dir_page.data, entry_idx, data_pid, free)
        self.pool.unpin_page(dir_id, True)
        return (data_pid, slot)

    def scan_raw(self): # scan the catalog and return the raw bytes
        from storage.page import SlottedPage, dir_get_next, dir_entry_count, dir_get_entry, NO_NEXT
        dir_id = 0
        while True:
            dir_page = self.pool.fetch_page(dir_id)
            count = dir_entry_count(dir_page.data)
            entries = [dir_get_entry(dir_page.data, i) for i in range(count)]
            next_id = dir_get_next(dir_page.data)
            self.pool.unpin_page(dir_id, False)
            for data_pid, _ in entries:
                page = self.pool.fetch_page(data_pid)
                sp = SlottedPage(page.data, page_id=data_pid)
                items = [(s, sp.get(s)) for s in sp.live_slots()]
                self.pool.unpin_page(data_pid, False)
                for slot, record in items:
                    yield (data_pid, slot), record
            if next_id == NO_NEXT:
                break
            dir_id = next_id


def SpFree(data, page_id): # get the free space of a slotted page
    from storage.page import SlottedPage
    return SlottedPage(data, page_id=page_id).free_space()


class Catalog:
    # db_dir holds catalog.db plus one {table}.db per table
    def __init__(self, db_dir: str | Path, frames: int = 16, policy: str = "lru"):
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.db_dir / "catalog.db"
        self.disk = DiskManager(self.catalog_path)
        self.pool = BufferPool(frames, policy, self.disk)
        self._heap = _RawHeap(self.pool, schema=[], table_name="__catalog__")

    def create_table(self, name: str, schema: list) -> None:
        if self.get_table(name) is not None:
            raise CatalogError(f"table {name!r} already exists")
        file_name = f"{name}.db"
        table_path = self.db_dir / file_name
        if table_path.exists():
            table_path.unlink()
        # spin up a tiny pool just long enough to create page 0 on the new file
        tdisk = DiskManager(table_path)
        tpool = BufferPool(4, "lru", tdisk)
        HeapFile(tpool, schema, table_name=name)
        tpool.flush_all()

        # then remember the table in catalog.db
        rec = _pack_table(name, schema, file_name, num_pages=1, num_tuples=0, stats=b"")
        self._heap.insert_raw(rec)
        self.pool.flush_all()

    def get_table(self, name: str) -> dict | None:
        for _rid, raw in self._heap.scan_raw():
            info = _unpack_table(raw)
            if info["name"] == name:
                return info
        return None

    def list_tables(self) -> list[str]:
        names = []
        for _rid, raw in self._heap.scan_raw():
            names.append(_unpack_table(raw)["name"])
        return names

    def update_table_stats(self, name: str, num_pages: int, num_tuples: int, stats: bytes = b"") -> None:
        # no in-place update — delete the old catalog row and write a new one
        found = None
        for rid, raw in self._heap.scan_raw():
            info = _unpack_table(raw)
            if info["name"] == name:
                found = (rid, info)
                break
        if found is None:
            raise CatalogError(f"unknown table {name!r}")
        rid, info = found
        self._heap.delete(rid)
        rec = _pack_table(
            name, info["schema"], info["file_name"],
            num_pages, num_tuples, stats if stats else info["stats"],
        )
        self._heap.insert_raw(rec)
        self.pool.flush_all()

    def open_heap(self, name: str, frames: int | None = None, policy: str = "lru") -> tuple[HeapFile, BufferPool, dict]:
        # look up metadata, then open that table's own disk + pool
        info = self.get_table(name)
        if info is None:
            raise CatalogError(f"unknown table {name!r}")
        path = self.db_dir / info["file_name"]
        disk = DiskManager(path)
        B = frames if frames is not None else self.pool.B
        pool = BufferPool(B, policy, disk)
        heap = HeapFile(pool, info["schema"], table_name=name)
        return heap, pool, info

    def flush(self) -> None:
        self.pool.flush_all()