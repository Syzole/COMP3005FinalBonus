"""Slotted page layout and record encoding (M1 spec §§2–3)."""

from __future__ import annotations

import struct
from storage.disk import PAGE_SIZE

HEADER_SIZE = 8  # page_id u32 + slot_count u16 + free_end u16
SLOT_SIZE = 4    # offset u16 + length u16
NO_SPACE = -1


class PageError(Exception):
    pass


def encode_int(value: int) -> bytes:
    return struct.pack("<i", value) #<i is a little endian integer


def decode_int(data: bytes, offset: int = 0) -> tuple[int, int]:
    return struct.unpack_from("<i", data, offset)[0], offset + 4 


def encode_varchar(value: str, max_len: int) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > max_len:
        raise PageError(f"VARCHAR value length {len(raw)} exceeds max {max_len}")
    return struct.pack("<H", len(raw)) + raw #<H is a little endian unsigned short 16 bit


def decode_varchar(data: bytes, offset: int = 0) -> tuple[str, int]:
    (length,) = struct.unpack_from("<H", data, offset)
    offset += 2
    raw = data[offset:offset + length]
    return raw.decode("utf-8"), offset + length


def encode_tuple(schema: list, values: list) -> bytes: # go from schema and values to bytes
    """schema entries: (name, 'INT') or (name, 'VARCHAR', n)."""
    if len(schema) != len(values):
        raise PageError(f"tuple arity {len(values)} != schema arity {len(schema)}")
    parts = []
    for col, val in zip(schema, values):
        kind = col[1]
        if kind == "INT":
            parts.append(encode_int(int(val)))
        elif kind == "VARCHAR":
            parts.append(encode_varchar(str(val), col[2]))
        else:
            raise PageError(f"unknown type {kind}")
    return b"".join(parts)


def decode_tuple(schema: list, data: bytes) -> list: # go from bytes to schema and values
    values = []
    offset = 0
    for col in schema:
        kind = col[1]
        if kind == "INT":
            v, offset = decode_int(data, offset)
            values.append(v)
        elif kind == "VARCHAR":
            v, offset = decode_varchar(data, offset)
            values.append(v)
        else:
            raise PageError(f"unknown type {kind}")
    return values

class SlottedPage:
    """In-memory view of one 4096-byte page with the M1 slot directory."""

    def __init__(self, data: bytearray | None = None, page_id: int = 0):
        if data is None:
            self.data = bytearray(PAGE_SIZE)
            self.page_id = page_id
            self.slot_count = 0
            self.free_end = PAGE_SIZE
            self._write_header()
        else:
            if len(data) != PAGE_SIZE:
                raise PageError(f"page must be {PAGE_SIZE} bytes")
            self.data = data if isinstance(data, bytearray) else bytearray(data)
            self._read_header()

    @classmethod
    def fresh(cls, data: bytearray, page_id: int) -> "SlottedPage":
        """Initialize an existing buffer as an empty slotted page."""
        if len(data) != PAGE_SIZE:
            raise PageError(f"page must be {PAGE_SIZE} bytes")
        data[:] = bytes(PAGE_SIZE)
        page = object.__new__(cls)
        page.data = data
        page.page_id = page_id
        page.slot_count = 0
        page.free_end = PAGE_SIZE
        page._write_header()
        return page


    def _read_header(self) -> None:
        self.page_id, self.slot_count, self.free_end = struct.unpack_from("<IHH", self.data, 0)

    def _write_header(self) -> None:
        struct.pack_into("<IHH", self.data, 0, self.page_id, self.slot_count, self.free_end)

    def free_space(self) -> int:
        return self.free_end - (HEADER_SIZE + SLOT_SIZE * self.slot_count)

    def _slot_offset(self, slot_no: int) -> int:
        return HEADER_SIZE + SLOT_SIZE * slot_no

    def get_slot(self, slot_no: int) -> tuple[int, int]:
        if slot_no < 0 or slot_no >= self.slot_count:
            raise PageError(f"slot {slot_no} out of range (slot_count={self.slot_count})")
        return struct.unpack_from("<HH", self.data, self._slot_offset(slot_no))

    def set_slot(self, slot_no: int, offset: int, length: int) -> None:
        struct.pack_into("<HH", self.data, self._slot_offset(slot_no), offset, length)

    def insert(self, record: bytes) -> int:
        L = len(record)
        if self.free_space() < L + SLOT_SIZE:
            return NO_SPACE
        self.free_end = self.free_end - L
        start = self.free_end
        self.data[start:start + L] = record
        slot_no = self.slot_count
        self.slot_count += 1
        self.set_slot(slot_no, start, L)
        self._write_header()
        return slot_no

    def get(self, slot_no: int) -> bytes:
        offset, length = self.get_slot(slot_no)
        if offset == 0:
            raise PageError(f"reading dead slot {slot_no}")
        return bytes(self.data[offset:offset + length])

    def delete(self, slot_no: int) -> None:
        offset, length = self.get_slot(slot_no)
        if offset == 0:
            raise PageError(f"slot {slot_no} already dead")
        self.set_slot(slot_no, 0, 0)
        # Space is NOT reclaimed (no compaction).

    def live_slots(self) -> list[int]:
        out = []
        for i in range(self.slot_count):
            offset, _ = self.get_slot(i)
            if offset != 0:
                out.append(i)
        return out

    def layout_lines(self) -> list[str]:
        lines = [
            f"page_id={self.page_id}",
            f"slot_count={self.slot_count}",
            f"free_end={self.free_end}",
            f"free_space={self.free_space()}",
            "slots:",
        ]
        for i in range(self.slot_count):
            off, length = self.get_slot(i)
            lines.append(f"  {i}:({off},{length})")
        return lines

