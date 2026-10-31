# How cases from Bonus 1 has changed

Projection no longer removes duplicate rows. Duplicate removal is the separate
`distinct(...)` operator. Set ops (`union` / `intersect` / `minus`) are still set-based.

## What has changed

| Case | Query | Why |
|------|-------|-----|
| **23** | `project[DID](Employees)` | Employees has two rows with `DID=D1`. Under set projection the result was `{D1, D2}`. Under bags it is `{D1, D2, D1}` (see `23/result.txt`). Use `distinct(project[DID](Employees))` for the old set meaning. |

## Unchanged under bags

Other cases in 1–25 either do not project collapsing duplicates, are error cases, or already expect bag-compatible output (e.g. single matching row after select+project).