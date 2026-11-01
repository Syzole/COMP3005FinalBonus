"""Heap file with free-space directory (M1 §4)."""

from __future__ import annotations

from storage.buffer import BufferPool
from storage.page import (
    SlottedPage, NO_SPACE,
    init_directory_page, init_data_page,
    dir_get_next, dir_set_next, dir_entry_count, dir_set_entry_count,
    dir_get_entry, dir_set_entry, DIR_CAPACITY, NO_NEXT,
    encode_tuple, decode_tuple,
)


class HeapError(Exception):
    pass


class HeapFile:
    def __init__(self, pool: BufferPool, schema: list, table_name: str = ""):
        self.pool = pool
        self.schema = schema
        self.table_name = table_name
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        if self.pool.disk.num_pages() == 0:
            page = self.pool.new_page()
            assert page.page_id == 0
            init_directory_page(page.data, 0)
            self.pool.unpin_page(0, True)

    def insert(self, values: list) -> tuple[int, int]:
        record = encode_tuple(self.schema, values)
        if len(record) > 4088:
            raise HeapError(
                f"record longer than 4088 bytes in table {self.table_name!r}"
            )
        need = len(record) + 4

        dir_id = 0
        while True:
            dir_page = self.pool.fetch_page(dir_id)
            count = dir_entry_count(dir_page.data)

            for i in range(count):
                data_pid, free_bytes = dir_get_entry(dir_page.data, i)
                if free_bytes >= need:
                    self.pool.unpin_page(dir_id, False)
                    return self._insert_into(data_pid, dir_id, i, record)

            next_id = dir_get_next(dir_page.data)
            if next_id != NO_NEXT:
                self.pool.unpin_page(dir_id, False)
                dir_id = next_id
                continue

            # Need a new data page (and maybe a new directory page).
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
            free = SlottedPage(data_page.data, page_id=data_pid).free_space()
            dir_set_entry(dir_page.data, count, data_pid, free)
            dir_set_entry_count(dir_page.data, count + 1)
            self.pool.unpin_page(data_pid, True)
            self.pool.unpin_page(dir_id, True)
            return self._insert_into(data_pid, dir_id, count, record)

    def _insert_into(
        self, data_pid: int, dir_id: int, entry_idx: int, record: bytes
    ) -> tuple[int, int]:
        page = self.pool.fetch_page(data_pid)
        sp = SlottedPage(page.data, page_id=data_pid)
        slot = sp.insert(record)
        if slot == NO_SPACE:
            self.pool.unpin_page(data_pid, False)
            raise HeapError("directory free_bytes lied; no space on data page")
        free = sp.free_space()
        self.pool.unpin_page(data_pid, True)

        dir_page = self.pool.fetch_page(dir_id)
        dir_set_entry(dir_page.data, entry_idx, data_pid, free)
        self.pool.unpin_page(dir_id, True)
        return (data_pid, slot)

    def get(self, rid: tuple[int, int]) -> list:
        page_id, slot = rid
        page = self.pool.fetch_page(page_id)
        try:
            record = SlottedPage(page.data, page_id=page_id).get(slot)
        except Exception as e:
            self.pool.unpin_page(page_id, False)
            raise HeapError(f"reading dead or invalid rid {rid}: {e}") from e
        self.pool.unpin_page(page_id, False)
        return decode_tuple(self.schema, record)

    def delete(self, rid: tuple[int, int]) -> None:
        page_id, slot = rid
        page = self.pool.fetch_page(page_id)
        sp = SlottedPage(page.data, page_id=page_id)
        try:
            sp.delete(slot)
        except Exception as e:
            self.pool.unpin_page(page_id, False)
            raise HeapError(str(e)) from e
        free = sp.free_space()
        self.pool.unpin_page(page_id, True)
        self._update_free(page_id, free)

    def _update_free(self, data_pid: int, free_bytes: int) -> None:
        dir_id = 0
        while True:
            dir_page = self.pool.fetch_page(dir_id)
            count = dir_entry_count(dir_page.data)
            for i in range(count):
                pid, _ = dir_get_entry(dir_page.data, i)
                if pid == data_pid:
                    dir_set_entry(dir_page.data, i, data_pid, free_bytes)
                    self.pool.unpin_page(dir_id, True)
                    return
            next_id = dir_get_next(dir_page.data)
            self.pool.unpin_page(dir_id, False)
            if next_id == NO_NEXT:
                raise HeapError(f"data page {data_pid} not in directory")
            dir_id = next_id

    def scan(self):
        """Yield (rid, tuple). At most one data page + one directory page pinned."""
        dir_id = 0
        while True:
            dir_page = self.pool.fetch_page(dir_id)
            count = dir_entry_count(dir_page.data)
            entries = [dir_get_entry(dir_page.data, i) for i in range(count)]
            next_id = dir_get_next(dir_page.data)
            self.pool.unpin_page(dir_id, False)

            for data_pid, _free in entries:
                page = self.pool.fetch_page(data_pid)
                sp = SlottedPage(page.data, page_id=data_pid)
                slots = list(sp.live_slots())
                records = [(s, sp.get(s)) for s in slots]
                self.pool.unpin_page(data_pid, False)
                for slot, record in records:
                    yield (data_pid, slot), decode_tuple(self.schema, record)

            if next_id == NO_NEXT:
                break
            dir_id = next_id

    def num_tuples(self) -> int:
        n = 0
        for _ in self.scan():
            n += 1
        return n
