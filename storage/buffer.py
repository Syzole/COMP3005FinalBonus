"""Buffer pool with LRU and CLOCK replacement."""

from __future__ import annotations

from storage.disk import DiskManager, PAGE_SIZE
from storage.page import SlottedPage


class BufferError(Exception):
    pass


class Frame:
    __slots__ = ("page_id", "data", "pin_count", "dirty", "ref")

    def __init__(self):
        self.page_id: int | None = None  # None = this frame is empty
        self.data: bytearray = bytearray(PAGE_SIZE)
        self.pin_count: int = 0  # >0 means someone is still using it — don't evict
        self.dirty: bool = False  # True => must write back before reuse
        self.ref: int = 0  # CLOCK only — "recently used" bit


class BufferPool:
    def __init__(self, frames: int, policy: str, disk: DiskManager):
        if frames < 1:
            raise BufferError("frames must be >= 1")
        policy = policy.lower()
        if policy not in ("lru", "clock"):
            raise BufferError(f"unknown policy {policy}")
        self.B = frames
        self.policy = policy
        self.disk = disk
        self.frames = [Frame() for _ in range(frames)]
        self.page_table: dict[int, int] = {}  # page_id -> which frame it's in
        self._lru: list[int] = []  # frame idxs; front = cold, end = hot
        self._hand = 0  # CLOCK clock hand
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def stats(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "reads": self.disk.reads,
            "writes": self.disk.writes,
        }

    def _on_access(self, frame_idx: int) -> None:
        # bump "recently used" depending on policy
        if self.policy == "lru":
            if frame_idx in self._lru:
                self._lru.remove(frame_idx)
            self._lru.append(frame_idx)
        else:
            self.frames[frame_idx].ref = 1

    def _choose_victim(self) -> int | None:
        if self.policy == "lru":
            # walk cold -> hot, first unpinned frame gets chosen
            for idx in self._lru:
                if self.frames[idx].pin_count == 0:
                    return idx
            # empty frames might not be in _lru yet
            for i, f in enumerate(self.frames):
                if f.page_id is None and f.pin_count == 0:
                    return i
            return None

        # CLOCK: start with ref=1,give second chances by clearing ref, then take first with ref=0
        swept = 0
        while swept <= 2 * self.B:
            f = self.frames[self._hand]
            idx = self._hand
            if f.pin_count > 0:
                self._hand = (self._hand + 1) % self.B
                swept += 1
            elif f.ref == 1:
                f.ref = 0
                self._hand = (self._hand + 1) % self.B
                swept += 1
            else:
                self._hand = (self._hand + 1) % self.B
                return idx
        return None

    def _find_free_or_victim(self) -> tuple[int, bool]:
        """(frame_idx, replaced) — replaced means we kicked something out."""
        for i, f in enumerate(self.frames):
            if f.page_id is None and f.pin_count == 0:
                return i, False
        victim = self._choose_victim()
        if victim is None:
            raise BufferError("no unpinned frame available")
        return victim, self.frames[victim].page_id is not None

    def _evict_if_needed(self, frame_idx: int, replaced: bool) -> None:
        f = self.frames[frame_idx]
        if f.page_id is not None:
            if f.dirty:
                self.disk.write_page(f.page_id, f.data)
                f.dirty = False
            del self.page_table[f.page_id]
            if self.policy == "lru" and frame_idx in self._lru:
                self._lru.remove(frame_idx)
            f.page_id = None
            if replaced:
                self.evictions += 1

    def fetch_page(self, page_id: int) -> SlottedPage:
        # hit: already in a frame — just pin + mark accessed
        if page_id in self.page_table:
            idx = self.page_table[page_id]
            f = self.frames[idx]
            f.pin_count += 1
            self._on_access(idx)
            self.hits += 1
            return SlottedPage(f.data, page_id=page_id)

        # miss: free a frame (maybe write dirty), then read from disk
        idx, replaced = self._find_free_or_victim()
        self._evict_if_needed(idx, replaced)

        data = self.disk.read_page(page_id)
        f = self.frames[idx]
        f.data[:] = data
        f.page_id = page_id
        f.pin_count = 1
        f.dirty = False
        self.page_table[page_id] = idx
        self.misses += 1
        self._on_access(idx)
        if self.policy == "clock":
            # loaded pages start with a free pass
            f.ref = 1
        return SlottedPage(f.data, page_id=page_id)

    def unpin_page(self, page_id: int, is_dirty: bool) -> None:
        # ALWAYS unpin when done or the pool fills up with pinned frames
        if page_id not in self.page_table:
            raise BufferError(f"page {page_id} not resident")
        f = self.frames[self.page_table[page_id]]
        if f.pin_count == 0:
            raise BufferError(f"unpinning page {page_id} at pin count 0")
        # once dirty, stays dirty until flush/evict
        f.dirty = f.dirty or is_dirty
        f.pin_count -= 1

    def new_page(self) -> SlottedPage:
        # allocate on disk first, then park it in a frame (starts dirty)
        page_id = self.disk.allocate_page()
        idx, replaced = self._find_free_or_victim()
        self._evict_if_needed(idx, replaced)

        f = self.frames[idx]
        f.data[:] = bytes(PAGE_SIZE)
        f.page_id = page_id
        f.pin_count = 1
        f.dirty = True
        self.page_table[page_id] = idx
        self._on_access(idx)
        if self.policy == "clock":
            f.ref = 1
        return SlottedPage.fresh(f.data, page_id)

    def flush_all(self) -> None:
        # push every dirty frame to disk (doesn't unpin anything)
        for f in self.frames:
            if f.page_id is not None and f.dirty:
                self.disk.write_page(f.page_id, f.data)
                f.dirty = False

    def get_frame_pin(self, page_id: int) -> int:
        if page_id not in self.page_table:
            return 0
        return self.frames[self.page_table[page_id]].pin_count

    def pinned_count(self) -> int:
        return sum(1 for f in self.frames if f.pin_count > 0)
