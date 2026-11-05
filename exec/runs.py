"""Temporary run files. Each page is one slotted page. Deleted on close."""

from __future__ import annotations

from pathlib import Path

from exec.budget import ExecError
from storage.disk import DiskManager, PAGE_SIZE
from storage.page import NO_SPACE, SlottedPage, encode_tuple


class RunFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.disk = DiskManager(self.path)
        self.alive = True

    def num_pages(self) -> int:
        return self.disk.num_pages()

    def write_page(self, data: bytes | bytearray, counter) -> None:
        pid = self.disk.allocate_page()
        self.disk.write_page(pid, data)
        counter.page_writes += 1

    def read_page(self, page_id: int, buf: bytearray, counter) -> None:
        data = self.disk.read_page(page_id)
        buf[:] = data
        counter.page_reads += 1

    def delete(self) -> None:
        if self.alive and self.path.exists():
            self.path.unlink()
        self.alive = False


def pack_tuples(schema, rows, budget, counter, ctx) -> RunFile:
    """Write rows into a new run, holding one frame."""
    run = ctx.new_run()
    if not rows:
        return run
    slot = ctx.slot()
    try:
        sp = SlottedPage.fresh(slot.buf, 0)
        for row in rows:
            rec = encode_tuple(schema, row)
            if len(rec) + 4 > PAGE_SIZE - 8:
                raise ExecError("tuple does not fit in a page")
            if sp.insert(rec) == NO_SPACE:
                run.write_page(slot.buf, counter)
                sp = SlottedPage.fresh(slot.buf, 0)
                if sp.insert(rec) == NO_SPACE:
                    raise ExecError("tuple does not fit in a page")
        if sp.slot_count > 0:
            run.write_page(slot.buf, counter)
    finally:
        slot.release()
    return run
