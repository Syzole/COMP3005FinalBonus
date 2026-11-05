"""Per-plan state: table metadata, the frame budget, and temp runs."""

from __future__ import annotations

from pathlib import Path

from storage.catalog import Catalog

from exec.budget import ExecError, Budget
from exec.runs import RunFile


class Context:
    def __init__(self, db_dir, frames: int):
        self.db_dir = Path(db_dir)
        self.budget = Budget(frames)
        self.tmp = self.db_dir / "_m2tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self.runs: list[RunFile] = []
        self.meta: dict[str, dict] = {}
        self._load_meta()

    def _load_meta(self) -> None:
        # One-frame catalog pool lives only for this lookup, then is dropped
        # before any operator acquires plan frames.
        cat = Catalog(self.db_dir, frames=1, policy="lru")
        try:
            for name in cat.list_tables():
                info = cat.get_table(name)
                if info is None:
                    continue
                self.meta[name] = {
                    "schema": [list(col) for col in info["schema"]],
                    "path": self.db_dir / info["file_name"],
                }
            cat.flush()
            if cat.pool.pinned_count() != 0:
                raise ExecError("catalog left a pinned frame")
        finally:
            cat.pool.flush_all()

    def new_run(self) -> RunFile:
        self._seq += 1
        path = self.tmp / f"run_{self._seq}.tmp"
        run = RunFile(path)
        self.runs.append(run)
        return run

    def slot(self):
        from exec.budget import PageSlot
        return PageSlot(self.budget)

    def cleanup(self) -> None:
        for run in self.runs:
            run.delete()
        self.runs.clear()
        if self.tmp.exists():
            for path in self.tmp.glob("*"):
                if path.is_file():
                    path.unlink()
            try:
                self.tmp.rmdir()
            except OSError:
                pass
