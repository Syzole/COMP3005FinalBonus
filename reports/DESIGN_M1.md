# DESIGN_M1 — Storage Engine and Buffer Pool

## Page size and endianness

Every file is a sequence of **4096-byte** pages. Multi-byte integers are **little-endian**. Page id 0 is the first page of a file. A record id is `(page_id, slot_no)`.

## Record format

No record header and no null bitmap. A record is the concatenation of field encodings in schema order:

| Type | Encoding |
|------|----------|
| `INT` | 4 bytes, signed two's complement |
| `VARCHAR(n)` | 2-byte unsigned length `L`, then `L` bytes UTF-8 (`L ≤ n ≤ 255`) |

Example `(a INT, b VARCHAR(5), c INT)` tuple `(42, 'bob', 7)`:

```
a   2A 00 00 00          4 bytes
b   03 00 62 6F 62       2 + 3 bytes
c   07 00 00 00          4 bytes
-------------------------------
record length            13 bytes
```

## Slotted page (data pages)

```
offset  size  field
     0     4  page_id          uint32
     4     2  slot_count       uint16   (includes dead slots)
     6     2  free_end         uint16   lowest byte used by record data
     8     4  slot 0           uint16 offset, uint16 length
    12     4  slot 1
   ...
                               <-- free space -->
 free_end                      record data (newest at lowest offset)
  4095                         end of page
```

- Free space = `free_end − (8 + 4 × slot_count)`.
- Insert of length `L` needs `L + 4` free bytes.
- Dead slot = `(offset=0, length=0)`. Slot numbers are never reused.
- Fixed-length capacity: `floor(4088 / (L + 4))`. For `L = 13` → **240**.

### Compaction

**Not implemented.** Deletes mark the slot dead and do not reclaim bytes until a future compaction. After deleting a 20-byte record, usable free space is unchanged (M1 §3.2).

### Worked example (must match `storage` / tests)

| After | slot_count | free_end | free space | slots |
|-------|------------|----------|------------|-------|
| empty | 0 | 4096 | 4088 | empty |
| insert 13 | 1 | 4083 | 4071 | 0:(4083,13) |
| insert 20 | 2 | 4063 | 4047 | 1:(4063,20) |
| insert 13 | 3 | 4050 | 4030 | 2:(4050,13) |
| delete slot 1 | 3 | 4050 | 4030 | 1:(0,0) |

### Real page from `data/db_m1_design/R.db` (page 1 after loading three 13-byte rows)

```
offset 0..7   header:  page_id=1, slot_count=3, free_end=4057
              bytes:   01 00 00 00  03 00  d9 0f
offset 8..19  slots:   0:(4083,13) 1:(4070,13) 2:(4057,13)
              bytes:   f3 0f 0d 00  e6 0f 0d 00  d9 0f 0d 00
offset 4057   newest (44,'sam',1): 2c 00 00 00 03 00 73 61 6d 01 00 00 00
offset 4070   middle (43,'amy',9): 2b 00 00 00 03 00 61 6d 79 09 00 00 00
offset 4083   oldest (42,'bob',7): 2a 00 00 00 03 00 62 6f 62 07 00 00 00
free_space = 4057 - (8 + 4*3) = 4037
```

## Heap file directory

Page 0 of every `<table>.db` is the first directory page:

```
offset  size  field
     0     4  page_id
     4     4  next_dir_page    0xFFFFFFFF if none
     8     2  entry_count
    10     6  entry 0          data_page_id u32, free_bytes u16
   ...
capacity = floor((4096 - 10) / 6) = 681 entries per directory page
```

**Free-space strategy:** first-fit walk of directory entries. If none fit, allocate a new data page and append an entry (or allocate a new directory page when full).

**Scan:** at most one directory page and one data page pinned at a time. Records are yielded after unpinning the data page before the next fetch.

## Buffer pool

Fixed array of `B` frames. Each frame: page bytes, pin count, dirty flag, CLOCK reference bit. Page table maps `page_id → frame`.

Counting rules:

- **hit** — fetch of a resident page
- **miss** — fetch that required a disk read
- **eviction** — a miss that replaced a resident page (first `B` fills are not evictions)
- **read / write** — calls into the disk manager

**LRU:** every fetch moves the frame to MRU; victim is LRU with pin 0.

**CLOCK:** exact sweep from M1 §5.2; `swept > 2B` → no victim (`no unpinned frame available`).

Pinned pages are never evicted. Unpin at pin count 0 is an error.

## Catalog (`catalog.db`)

Stored as a heap of opaque binary records (same directory + slotted pages), accessed only through the buffer pool.

Per-table record:

```
u16 name_len + name
u16 ncol
  repeated: u16 col_name_len + name, u8 type_tag (0=INT,1=VARCHAR), u8 vmax
u16 file_len + file_name
u32 num_pages
u32 num_tuples
u16 stats_len + stats blob   # M4 ANALYZE marker / sidecar pointer
```

This leaves an extensible `stats` blob for histograms / ndv / min / max / width without breaking the catalog format. Full histograms live in `{table}.stats.json` when they exceed the 4088-byte record limit.

## Files on disk

```
<db>/
  catalog.db
  <table>.db
  <table>.stats.json   # optional, after ANALYZE
```

## CLI

```
python -m storage create-table --db DIR --name R --schema "a:INT,b:VARCHAR(20),c:INT"
python -m storage load         --db DIR --table R --file r.csv
python -m storage scan         --db DIR --table R --frames 8 [--limit N] --json
python -m storage layout       --db DIR --table R --page N
python -m storage trace        --db DIR --file trace.txt --frames B --policy lru|clock --json
```

## Readings

1. **Ramakrishnan and Gehrke, ch. 9** — slotted pages, heap files, and free-space directories; motivated first-fit directory entries and stable RIDs without compaction.
2. **Silberschatz et al., ch. 12–13** / **O'Neil et al., LRU-K (SIGMOD 1993)** — buffer-pool replacement; CLOCK as an approximation of LRU and why sequential flooding defeats plain LRU (REPORT_M1 Trace 3).
