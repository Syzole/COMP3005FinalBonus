# REPORT_M4 — Optimizer Measurements

Workload: synthetic `data/m4_workload/` (800 Emp rows; **region** ≈ **band** correlated).  
Regenerate: `python scripts/gen_m4_workload.py` then `python scripts/measure_m4.py`  
Raw: `data/m4_measurements.json`.

## 1. Estimation quality (q-error) — every node

Over 20 queries, **76 plan nodes** compared explain `est_rows` to executor `tuples_out` (same run):

| Metric | All nodes | Root only |
|--------|----------:|----------:|
| Median q-error | **1.0** | **1.4** |
| Worst q-error | **152** | **152** |

Seqscans sit at 1.0. Filters on uncorrelated columns stay near 1. The worst case is the correlated conjunction (below). Equality on `region` / `band` alone underestimates (~8×) because histogram NDV cuts are coarse relative to the true ~1/5 of 800 rows.

## 2. Does optimization pay?

Ratio = (naive I/O) / (optimized I/O), naive = no rewrite + fixed `hash_join` / written order (Cartesian NLJ + residual filter for comma joins).

| | |
|--|--:|
| Min ratio | **0.21** |
| Max ratio | **1.0** |

Most equality joins already match the cost model’s preferred algorithm on these small tables (ratio ≈ 1). The low end is `SELECT DISTINCT did FROM Emp`, where the optimizer picks **hash** distinct while naive uses **sort**. Pushdown still changes plan shape (comma → join, filters under children); on the correlated comma join, naive Cartesian NLJ uses **9** page I/Os and **32 000** comparisons vs optimized **7** I/Os and **~4 000** comparisons (see §4).

## 3. Which rule mattered (ablation)

Turn off one rewrite rule at a time; sum page I/O over the join/comma subset of the workload (`baseline_io` = 133 with all rules):

| Disabled rule | Total I/O | Δ vs baseline | Notes |
|--------------:|----------:|--------------:|-------|
| 1 split AND | 125 | −8 | Some plans fail open without split; survivors cheaper but wrong shape risk |
| 2 push select | 125 | −8 | Same coupling with 1/3 on this set |
| 3 cross→join | 125 | −8 | Largest *semantic* regression for comma joins when it fails |
| 4 push rename/union | 133 | 0 | Rare on this workload |
| 5 push below group_by | 133 | 0 | Few HAVING pushes |
| 6 project push | 120 | −13 | Width/I/O effect small; count differs when a plan errors |
| 7 cleanup | 133 | 0 | Cosmetic |

On **tiny** tables absolute I/O barely moves; rule **3** is still the one that makes comma and `JOIN ON` meet. Confirmed by `tests/m4/test_opt.py` (`test_rewrite_comma_to_join`, `test_comma_and_join_same_physical_shape`).

## 4. Where independence breaks (highest weight)

Query: `SELECT eid FROM Emp WHERE region = 3 AND band = 3;`

| | |
|--|--|
| Assumption | `sel(p∧q)=sel(p)·sel(q)` |
| Independent estimate | ~0.5 rows |
| Optimizer `est_rows` | **1** |
| Actual rows | **152** |
| q-error | **152** |

**What failed:** `region` and `band` are generated equal ~95% of the time. Independence underestimates the joint selectivity.

**What would help:** a 2D histogram / correlation statistic on `(region, band)`, or detecting functional dependency.

**Cost impact (join embedding):**  
`SELECT e.eid FROM Emp e, Dept d WHERE e.did=d.did AND e.region=3 AND e.band=3;`

| Plan | Page I/O | Comparisons | Rows |
|------|--------:|------------:|-----:|
| Optimized (rewrite + DP) | **7** | **~4000** | 152 |
| Naive (cross + filter, no pushdown) | **9** | **32000** | 152 |
| I/O delta (bad − better) | **+2** | ×8 comparisons | |

Single-table shape is still scan+filter (I/O unchanged); the underestimate hurts when residual predicates sit above a Cartesian product or when join order trusts a near-empty build/probe side.

## 5. DP table (4 relations)

Query:

```sql
SELECT e.eid FROM Emp e, Dept d, Proj p, Assign a
WHERE e.did = d.did AND p.did = d.did AND a.eid = e.eid AND a.pid = p.pid;
```

Logical JSON: `data/m4_workload/four_join.json`.  
Command: `python -m opt dp-table --db data/m4_workload/db --logical data/m4_workload/four_join.json --frames 32`

`opt plan` uses the same DP enumeration for join order (not display-only).

```
{a}  cost=6.0  rows=1500  op=seqscan
{d}  cost=1.0  rows=20  op=seqscan
{e}  cost=6.0  rows=800  op=seqscan
{p}  cost=1.0  rows=100  op=seqscan
{a,d}  cost=7.0  rows=30000  op=nested_loop_join
{a,e}  cost=12.0  rows=1200000  op=nested_loop_join
{a,p}  cost=7.0  rows=1875  op=nested_loop_join
{d,e}  cost=7.0  rows=16000  op=nested_loop_join
{d,p}  cost=2.0  rows=2000  op=nested_loop_join
{e,p}  cost=7.0  rows=80000  op=nested_loop_join
{a,d,e}  cost=96.0  rows=24000000  op=nested_loop_join
{a,d,p}  cost=14.0  rows=37500  op=nested_loop_join
{a,e,p}  cost=431.0  rows=1500000  op=nested_loop_join
{d,e,p}  cost=14.0  rows=1600000  op=nested_loop_join
{a,d,e,p}  cost=8456.0  rows=30000000  op=nested_loop_join
```

## Checklist

- Emp/Dept rewrite example reproduces (join + pushed filters).
- ANALYZE uses M2 external sort; twice identical.
- Physical plans validate and run in M2; DP chooses join order in `opt plan`.
- Comma vs JOIN ON both become joins.
- Cost model changes preferred algorithm as B and sizes change.
- No-stats path still plans.

## Readings

1. **Selinger et al., Access Path Selection (SIGMOD 1979)** — left-deep DP.
2. **Leis et al., How Good Are Query Optimizers, Really? (VLDB 2015)** — q-error framing for §1.
