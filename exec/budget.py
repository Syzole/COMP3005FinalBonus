"""One shared page budget for a whole plan."""

from __future__ import annotations

from storage.disk import PAGE_SIZE


class ExecError(Exception):
    pass


class Budget:
    """Counts live 4096-byte frames. acquire fails past B."""

    def __init__(self, frames: int):
        if frames < 1:
            raise ExecError("frames must be >= 1")
        self.B = frames
        self.held = 0
        self.peak = 0

    def acquire(self, n: int = 1) -> None:
        if n < 0:
            raise ExecError("negative frame acquire")
        if self.held + n > self.B:
            raise ExecError(
                f"memory budget exceeded ({self.held}+{n} > {self.B})"
            )
        self.held += n
        if self.held > self.peak:
            self.peak = self.held

    def release(self, n: int = 1) -> None:
        if n > self.held:
            raise ExecError("budget release underflow")
        self.held -= n


class PageSlot:
    """One acquired frame."""

    def __init__(self, budget: Budget):
        budget.acquire(1)
        self.budget = budget
        self.buf = bytearray(PAGE_SIZE)
        self.live = True

    def release(self) -> None:
        if self.live:
            self.budget.release(1)
            self.live = False
            self.buf = None


class Charge:
    """Charge n frames for tuple bytes that are not sitting in a PageSlot."""

    def __init__(self, budget: Budget, n: int):
        self.budget = budget
        self.n = n
        self.live = False
        if n > 0:
            budget.acquire(n)
            self.live = True

    def release(self) -> None:
        if self.live:
            self.budget.release(self.n)
            self.live = False
