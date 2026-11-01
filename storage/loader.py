"""CSV bulk loader (M1 §6)."""

from __future__ import annotations

from pathlib import Path
from storage.catalog import Catalog, CatalogError
from storage.heap import HeapError


class LoadError(Exception):
    pass


def parse_schema(spec: str) -> list:
    """Parse 'a:INT,b:VARCHAR(20),c:INT' into schema list."""
    schema = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise LoadError(f"bad schema field {part!r}")
        name, typ = part.split(":", 1)
        name = name.strip()
        typ = typ.strip().upper()
        if typ == "INT":
            schema.append([name, "INT"])
        elif typ.startswith("VARCHAR"):
            # VARCHAR(n)
            if not (typ.startswith("VARCHAR(") and typ.endswith(")")):
                raise LoadError(f"bad VARCHAR type {typ!r}")
            n = int(typ[len("VARCHAR("):-1])
            if n < 1 or n > 255:
                raise LoadError(f"VARCHAR length {n} out of range 1..255")
            schema.append([name, "VARCHAR", n])
        else:
            raise LoadError(f"unknown type {typ!r}")
    if not schema:
        raise LoadError("empty schema")
    return schema


def parse_csv_line(line: str) -> list[str]:
    """Split a CSV line; fields with commas must be double-quoted."""
    fields = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] == '"':
            i += 1
            buf = []
            while i < n:
                if line[i] == '"':
                    if i + 1 < n and line[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(line[i])
                i += 1
            fields.append("".join(buf))
            if i < n and line[i] == ",":
                i += 1
            continue
        # unquoted
        j = i
        while j < n and line[j] != ",":
            j += 1
        fields.append(line[i:j])
        i = j + 1 if j < n else j
    return fields


def load_csv(catalog: Catalog, table: str, csv_path: str | Path, frames: int = 32, policy: str = "lru") -> int:
    info = catalog.get_table(table)
    if info is None:
        raise CatalogError(f"unknown table {table!r}")
    schema = info["schema"]
    heap, pool, _ = catalog.open_heap(table, frames=frames, policy=policy)

    path = Path(csv_path)
    count = 0
    with path.open(encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.rstrip("\n\r")
            if line == "":
                continue
            fields = parse_csv_line(line)
            if len(fields) != len(schema):
                raise LoadError(
                    f"line {line_no}: expected {len(schema)} fields, got {len(fields)}"
                )
            values = []
            for col_i, (col, field) in enumerate(zip(schema, fields)):
                if col[1] == "INT":
                    try:
                        values.append(int(field))
                    except ValueError as e:
                        raise LoadError(
                            f"line {line_no}, column {col[0]}: bad INT {field!r}"
                        ) from e
                else:
                    if len(field.encode("utf-8")) > col[2]:
                        raise LoadError(
                            f"line {line_no}, column {col[0]}: VARCHAR longer than {col[2]}"
                        )
                    values.append(field)
            try:
                heap.insert(values)
            except HeapError as e:
                raise LoadError(f"line {line_no}: {e}") from e
            count += 1

    pool.flush_all()
    num_pages = pool.disk.num_pages()
    catalog.update_table_stats(table, num_pages=num_pages, num_tuples=count)
    return count
