# ARCHITECTURE — COMP3005 FinalBonus

One system, four contracts. Integration is wiring; the module CLIs still work alone.

```
 SQL text / shell meta-commands
            |
            v
   [ M3 ]  sql/   lexer → parser → resolve → translate
            |  logical plan JSON
            v
   [ M4 ]  opt/   ANALYZE stats → rewrite 1–7 → Selinger DP
            |       (join order + algorithm) → physical JSON
            v
   [ M2 ]  exec/  iterator tree, open/next/close, frame budget B
            |  page requests
            v
   [ M1 ]  storage/  buffer pool → heap files → 4 KB pages
            ^
            |
     catalog + {table}.stats.json  (read by M4)
```

## Module boundaries

| From → To | Contract |
|-----------|----------|
| Shell / tests → M3 | SQL string + catalog → logical plan JSON (`scan`/`select`/`join`/…, never `hash_join`) |
| M3 → M4 | Same logical JSON |
| M4 → M2 | Physical plan JSON (`seqscan`, `filter`, `nested_loop_join`, `hash_join`, `sort_merge_join`, …) |
| M2 → M1 | `Catalog` / `HeapFile` / `BufferPool` only — M2 never opens files by path for table data except through storage |
| M4 → M1 | `ANALYZE` scans heaps; stats sidecar + catalog counters |

Shared library: `engine/` (condition parser, AST, bag RA helpers) used by M2–M4. No printing or file I/O inside `engine/`.

## CLIs (still standalone)

```
python -m storage …
python -m exec …
python -m sql …
python -m opt …
python -m shell --db DIR [--frames N]   # integration
```

Shell meta: `SET frames = N`, `LOAD t FROM 'f.csv'`, `ANALYZE t`, `EXPLAIN …`, `EXPLAIN ANALYZE …`, plus the SQL subset.

## What I would do differently

1. **Catalog stats in-page from day one** — the 4088-byte record limit forced a sidecar; a dedicated stats heap would have been cleaner.
2. **One iterator budget object shared with ANALYZE** — ANALYZE currently uses M2 `Sort` via a separate `Context`; unifying pools would simplify oral demos of frame accounting.
3. **Bushy DP optional** — left-deep is enough for ≤6 relations, but a few star queries want a bushy hash build.
