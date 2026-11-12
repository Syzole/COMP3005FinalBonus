# DESIGN_M4 — Query Optimizer

## Pipeline

Logical JSON (M3) → **rewrite (rules 1–7)** → **cost / join choice** → physical JSON (M2).  
`ANALYZE` fills per-table stats used by selectivity and DP.

CLI: `python -m opt analyze|rewrite|plan|explain|dp-table …`

## Statistics layout

Full histograms do not fit in the 4088-byte catalog record limit. We store:

- Catalog `stats` blob = marker `STATS_FILE_V1`
- Sidecar file `{db}/{table}.stats.json` with:

```json
{
  "n_tuples": ..., "n_pages": ..., "width": ...,
  "buckets": 20,
  "columns": {
    "age": {"ndv": ..., "min": ..., "max": ..., "hist": [{"lo","hi","count","ndv"}, ...]}
  }
}
```

Equi-depth histograms: during the single heap scan each column is spilled to an M2 temp run; then `exec.ops.Sort` (external merge sort) orders the run and we stream `next()` to cut every `n/k` tuples and count NDV via adjacent runs. No full-column `sorted()` in memory. `ANALYZE` twice yields identical JSON.

### No-stats defaults

If ANALYZE was never run: `n_tuples`/`n_pages` from catalog counters; `ndv = max(1, n/10)`; no hist → equality sel = `1/ndv`; range sel ≈ 0.33.

## Selectivity (implemented defaults)

As in M4spec §2: hist equality, range with bucket fractions, `AND` product (independence), `OR` inclusion, `NOT`, join `n(R)*n(S)/max(ndv)`, other join preds **0.1**. Cardinality `ceil(rows*sel)` floored at 1; pages `ceil(rows*width/4096)`.

## Rewrite rules

| # | Action | Precondition |
|---|--------|--------------|
| 1 | Split AND into stacked selects | always (unwrap parens so nested ANDs flatten) |
| 2 | Push select under join/cross | predicate refs one side only |
| 3 | cross+select → join | single equality touching both sides |
| 4 | Push select under rename / union | rewrite through rename; both union branches |
| 5 | Push select under group_by | grouping columns only (not aggregates) |
| 6 | Insert projections under join | keep ancestor + join-key columns |
| 7 | Drop identity-ish duplicate projects | cosmetic |

Comma join and `JOIN ON` become the same join shape after 1–3.

## Cost model

Page I/O only, matching M2: seqscan M; sort `2M(passes-1)+M`; NLJ `M+⌈M/(B-2)⌉N`; SMJ `sort(M)+sort(N)+M+N`; hash `3(M+N)`. Inequality joins → NLJ only. Distinct/group_by prefer `hash` when build fits in B−1 pages else `sort`.

## Join enumeration

Left-deep Selinger DP over ≤6 base relations (`opt/dp.py`). After rewrite, `opt plan` / `opt explain` extract the join/cross cluster (access paths = scan + local filters/projects/renames; edges = equality join preds), physicalize each access path, then call `optimize_joins` so **both join order and algorithm** come from the DP table. `opt dp-table` uses the same extraction so the printed table matches the plan that is emitted.

**Why left-deep:** keeps the outer input pipelineable for NLJ; search space `O(n·2^n)` instead of bushy catalan growth. **Lost:** some bushy plans that reduce intermediate size for hash joins on large bushy trees.

## Physical mapping

| Logical | Physical |
|---------|----------|
| scan (+ rename) | seqscan (`as` = alias) |
| select | filter |
| project | project |
| join / cross | nested_loop_join \| sort_merge_join \| hash_join |
| group_by / distinct | + `mode`; aggregates as `[fn, arg]` or `[fn, arg, alias]` |
| sort / limit / setops | same names |

## Readings

1. **Selinger et al., Access Path Selection… (SIGMOD 1979)** — DP left-deep join order; motivated our `dp.py` structure.
2. **Ioannidis & Christodoulakis / histogram literature** — equi-depth hist for range sel; independence failure is the REPORT_M4 correlation case.
