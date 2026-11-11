"""Module 4 — query optimizer.

Logical plan (M3 JSON) in → rewrite → cost / join choice → physical plan (M2 JSON).

Package layout (built as we go):
  rewrite.py     — algebraic rewrite rules 1–7 (this milestone's first piece)
  analyze.py     — ANALYZE → catalog stats / histograms   (next)
  selectivity.py — predicate selectivity from stats
  cost.py        — page-I/O cost model (must match M2)
  dp.py          — left-deep Selinger join enumeration
  physicalize.py — logical → physical ops for the executor
  cli.py         — `python -m opt …`
"""
