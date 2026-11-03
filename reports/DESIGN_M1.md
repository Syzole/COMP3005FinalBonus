# Design M1 — Page layout

## Page size

Every file is a sequence of 4096-byte pages. Multi-byte integers are little-endian. Page id 0 is the first page. A record id (RID) is `(page_id, slot_no)`.

## Slotted page layout (byte-level)

Each data page is a slotted page: header + slot directory grow from the left; records grow from the right; free space is the gap in the middle.

```
byte:  0        8                    slot_end          free_end              4096
       |--------|--------------------|-----------------|-----------------------|
       | header | slot directory     |    free space   | records (packed)      |
       |  8 B   | N × 4 B            |                 | from free_end → 4095  |
       |--------|--------------------|-----------------|-----------------------|
                 ---- grow right --->                   <---- grow left -------
```

- `slot_end = 8 + 4 × slot_count`
- `free_space = free_end − slot_end`
- An insert fits only if that gap has room for the record bytes plus one new 4-byte slot.



### Page header (8 bytes at offset 0)

Packed little-endian as `"<IHH"`:

```
 0               4         6         8
 |---------------|---------|---------|
 | page_id (u32) | slot_ct | free_end|
 |---------------|---------|---------|
```


| Offset | Size | Type | Field        | Meaning                                                                  |
| ------ | ---- | ---- | ------------ | ------------------------------------------------------------------------ |
| 0      | 4    | u32  | `page_id`    | identity of this page                                                    |
| 4      | 2    | u16  | `slot_count` | number of slot entries (including dead ones since we dont do compaction) |
| 6      | 2    | u16  | `free_end`   | first byte of the contiguous record region; empty page uses `4096`       |




### Slot directory (4 bytes per slot, starts at offset 8)

Slot `i` is at `8 + 4×i`, packed `"<HH"`:

```
slot i: | offset (u16) | length (u16) |   = 4 B
```


| Field    | Type | Meaning                                    |
| -------- | ---- | ------------------------------------------ |
| `offset` | u16  | absolute byte offset of the record payload |
| `length` | u16  | payload length in bytes                    |


- A live slot has `offset ≠ 0` (records never start at byte 0; that region is the header).
- A dead slot is `(offset=0, length=0)` after delete. The RID stays reserved, but `get` fails on it.



### Record format (on-page payload)

A tuple is the concatenation of column encodings. There is no row-length prefix; decoding needs the table schema.


| Type         | Encoding                               | Bytes                         |
| ------------ | -------------------------------------- | ----------------------------- |
| `INT`        | signed 32-bit LE `"<i"`                | 4                             |
| `VARCHAR(n)` | u16 LE byte-length, then UTF-8 payload | `2 + len(utf8)` (payload ≤ n) |


Example: schema `(a INT, b VARCHAR(5), c INT)` with values `(7, "bob", 9)`:

```
| a: INT      | b: VARCHAR           | c: INT      |
| 07 00 00 00 | 03 00 62 6f 62       | 09 00 00 00 |
| 4 B         | 2 + 3 = 5 B          | 4 B         |  total L = 13
```

The slot stores `(offset, L)` for that whole blob. Column names/types are not stored on the page.

## Free space strategy



### Insert

1. Require `free_space ≥ L + 4` (payload + new slot entry).
2. Place the record at the high end: `free_end ← free_end − L`, write `L` bytes there.
3. Append a slot pointing at `(free_end, L)`, bump `slot_count`, rewrite the header.
4. If there is not enough space, return no-space; the heap finds or allocates another data page.



### Delete

- Mark as dead only: set the slot to `(0, 0)`.
- Do not move other records, shrink `slot_count`, or raise `free_end`.
- Deleted bytes remain in the record region; the middle free gap does not grow, this was due to limited time as I didnt have much time to search compaction strats



### Read

- Follow the slot’s `(offset, length)` and slice those bytes; reject dead slots (`offset == 0`).



## Reasoning

A slotted layout keeps variable-length `VARCHAR` records addressable by stable RIDs: clients hold `(page_id, slot_no)`, and only the slot’s offset/length would need to change if we ever compacted.

The header is minimal on purpose: `page_id` for checks, `slot_count` to bound the directory, and `free_end` so the next insert location is O(1) without scanning. Slots on the left and records on the right leave one contiguous free gap, so the fit check is a single subtraction.

Deletes are lazy delete marks instead of in-page compaction because M1 insert/scan do not need to reclaim holes inside a page, and compaction would complicate RID stability and buffer dirtying for little benifit. The tradeoff is that a page can look full (`free_space` small) while still holding dead record bytes; overflow is handled by the heap’s free-byte directory and new-page allocation rather than page reorganization, saving me from complexity.