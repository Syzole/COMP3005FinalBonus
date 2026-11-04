"""Execution context: catalog, frame budget, temp files."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from storage.catalog import Catalog
from storage.disk import PAGE_SIZE, DiskManager
from storage.buffer import BufferPool
from storage.heap import HeapFile
from storage.page import encode_tuple


class FrameBudgetError(Exception):
    pass


class FrameBudget: # soft tracker so we dont exceed B
    """Track soft frame holdings for the whole plan. Peak must never exceed B."""

    def __init__(self, B: int):
        if B < 1:
            raise FrameBudgetError("B must be >= 1")
        self.B = B
        self.held = 0
        self.peak = 0

    def acquire(self, n: int) -> None: #ops claim frames at open
        if n < 0:
            raise FrameBudgetError("negative acquire")
        if self.held + n > self.B:
            raise FrameBudgetError(
                f"frame budget exceeded: held={self.held} + {n} > B={self.B}"
            )
        self.held += n
        if self.held > self.peak:
            self.peak = self.held

    def release(self, n: int) -> None: #ops release frames at close
        if n < 0 or n > self.held:
            raise FrameBudgetError(f"bad release {n} (held={self.held})")
        self.held -= n

    def frames_for_bytes(self, nbytes: int) -> int: #convert tuple bytes to frames
        if nbytes <= 0:
            return 0
        return math.ceil(nbytes / PAGE_SIZE)


def tuple_nbytes(schema: list, values: list) -> int:
    return len(encode_tuple(schema, values))


class ExecContext: # a single plan run , catalog, budget, temp files and opened heaps
    """Shared state for one plan run."""

    def __init__(self, db_dir: str | Path, frames: int, policy: str = "lru"):
        self.db_dir = Path(db_dir)
        self.B = frames
        self.policy = policy
        self.catalog = Catalog(self.db_dir, frames=min(8, max(4, frames)), policy=policy)
        self.budget = FrameBudget(frames)
        self.temp_dir = Path(tempfile.mkdtemp(prefix="exec_m2_"))
        self._temp_paths: list[Path] = []
        self._heaps: list[tuple[HeapFile, BufferPool]] = []
        self._temp_seq = 0

    def new_temp_path(self, prefix: str = "run") -> Path: #create a new temp file for the plan run
        self._temp_seq += 1
        path = self.temp_dir / f"{prefix}_{self._temp_seq}.db"
        self._temp_paths.append(path)
        return path

    def open_table(self, name: str, pool_frames: int = 2) -> tuple[HeapFile, BufferPool, dict]: #open a table heap with a small dedicated buffer pool (counted elsewhere).
        """Open a table heap with a small dedicated buffer pool (counted elsewhere)."""
        info = self.catalog.get_table(name)
        if info is None:
            from exec.iterator import ExecError
            raise ExecError(f"unknown table {name!r}")
        path = self.db_dir / info["file_name"]
        disk = DiskManager(path)
        # Seqscan needs at most 2 pins (dir + data). Give pool that many frames.
        pf = max(2, pool_frames)
        pool = BufferPool(pf, self.policy, disk)
        # Qualify schema with table name for executor use.
        qschema = []
        for col in info["schema"]:
            cname = col[0]
            if "." in cname:
                q = list(col)
            else:
                q = [f"{name}.{cname}"] + list(col[1:])
            qschema.append(q)
        heap = HeapFile(pool, info["schema"], table_name=name)
        self._heaps.append((heap, pool))
        return heap, pool, {**info, "qschema": qschema}

    def cleanup(self) -> None: #flush all buffers and delete all temp files and temp dirs.
        for _heap, pool in self._heaps:
            try:
                pool.flush_all()
            except Exception:
                pass
        self._heaps.clear()
        for p in self._temp_paths:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        self._temp_paths.clear()
        try:
            if self.temp_dir.exists():
                for child in self.temp_dir.iterdir():
                    child.unlink(missing_ok=True)
                self.temp_dir.rmdir()
        except OSError:
            pass
