# DESIGN_LOG — COMP3005 Final Bonus

Diary plus the required “AI was wrong” entries (≥2 per module). A log that only says “I asked and it worked” scores zero — these are the places that cost real debugging time.

---

## Week-1 engine refactor / M0

Separated Bonus 1 into `engine/` (tokeniser, parser, tree, eval, JSON). Bags: `project` keeps duplicates; `distinct` is separate. Bonus 1 case 23 was updated for bag projection.

### AI wrong

1. Suggested keeping set-semantics projection “because RA is sets” and emitting an implicit distinct after every project. That would have made SQL `SELECT` dedup. Caught by re-reading general §1.3 point 6 and the bag note in M3.
2. First JSON round-trip dropped qualified names into bare column strings. Caught by `tests/m1/test_json.py` failing on a `table.column` project list.

---

## Module 1 — Storage

Implemented disk, slotted pages, heap directory (first-fit), buffer pool (LRU+CLOCK), catalog, loader, `python -m storage`.

### AI wrong

1. Early CLOCK victim loop advanced the hand only on ref=1 clear and forgot to advance after selecting a victim — §5.3 CLOCK totals drifted (wrong hits/evictions). Caught by the worked example table in `tests/m1/test_buffer.py`.
2. Suggested reclaiming free space on delete by sliding records (compaction). Spec §3.2 forbids that for this module; after delete of slot 1, free space must stay unchanged. Caught by the layout worked example.
3. Catalog design that stuffed a full text schema dump into page 0 without a heap — broke when table count grew. Replaced with opaque heap records + length-prefixed stats blob (room for M4).

Brightspace traces were missing; synthetic traces via `scripts/gen_traces.py` feed REPORT_M1.

---

## Module 2 — Execution

Iterator operators, external sort, BNL/SMJ/HJ, distinct/group (sort+hash), set ops, budget, `python -m exec`.

### AI wrong

1. Sized sort pass-0 by raw bytes `B * 4096` instead of slotted page packing. Looked fine until the §2.5 example produced 2 passes instead of 3 (22 reads / 14 writes). Fixed by filling B slotted pages per run.
2. Distinct/Sort parent counters also summed children in `totals`, double-counting I/O in `run --json`. Caught by comparing a seqscan→sort plan’s disk reads to the buffer pool.
3. Hash-join sketch built an unbounded in-memory table “when it fits” with no page accounting — correct tuples, not allowed under B. Replaced with Grace partitioning + BNL overflow fallback.
4. PowerShell `Set-Content` wrote a UTF-8 BOM; `json.loads` failed on plans. `load_file` now uses `utf-8-sig`.

---

## Module 3 — SQL front end

Lexer/parser/resolve/translate, `GRAMMAR_SQL.md`, fifteen required cases.

### AI wrong

1. HAVING condition kept as `COUNT(*) > 5` in the plan; §5.4 requires the output alias (`n > 5`). Caught by matching the worked example tree.
2. Proposed forbidding aggregates in the condition grammar entirely — then `HAVING COUNT(*)` became a syntax error. Aggregates are legal operands; WHERE rejects them in resolve, not the parser.
3. Grammar sketch showed only `[ AS ident ]` for aliases; examples use `FROM Emp e` without `AS`. Parser accepts bare aliases.

---

## Module 4 — Optimizer

ANALYZE (sidecar stats), selectivity, rewrite 1–7, cost model, Selinger DP wired into **`opt plan`** (not only `dp-table`), physicalize → M2.

### AI wrong

1. Storing full histogram JSON in the catalog `stats` blob hit `catalog record too large`. Sidecar `{table}.stats.json` + marker blob fixed it.
2. First AND-split did not unwrap nested parentheses, so rule 3 saw a mangled residual and comma joins never became joins. Caught by Emp/Dept rewrite smoke test.
3. Suggested leaving DP for `dp-table` only and picking algorithms per written join node in `plan`. That fails M4 §5 (order is the point). Fixed by extracting the join cluster and calling `optimize_joins` inside `plan_physical`.
4. ANALYZE used `sorted(vals)` in RAM; spec requires M2 external sort. Now spill columns to temp runs and sort with `exec.ops.Sort`.

---

## Integration

Shell (`python -m shell`) wires M3→M4→M2→M1 with EXPLAIN / EXPLAIN ANALYZE / ANALYZE / SET frames / LOAD. `ARCHITECTURE.md` describes module boundaries. Module CLIs remain unchanged.
