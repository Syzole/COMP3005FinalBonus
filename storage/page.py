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