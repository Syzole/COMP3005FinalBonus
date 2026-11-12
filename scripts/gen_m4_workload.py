"""Synthetic M4 workload: correlated columns + 20 queries."""

from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.catalog import Catalog
from storage.loader import load_csv
from opt.analyze import analyze_table

OUT = ROOT / "data" / "m4_workload"
DB = OUT / "db"


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    DB.mkdir()
    cat = Catalog(DB)
    cat.create_table(
        "Emp",
        [
            ["eid", "INT"],
            ["did", "INT"],
            ["age", "INT"],
            ["salary", "INT"],
            ["region", "INT"],  # correlated with salary band
            ["band", "INT"],  # strongly correlated with region
        ],
    )
    cat.create_table("Dept", [["did", "INT"], ["budget", "INT"], ["city", "INT"]])
    cat.create_table("Proj", [["pid", "INT"], ["did", "INT"], ["cost", "INT"]])
    cat.create_table("Assign", [["eid", "INT"], ["pid", "INT"], ["hours", "INT"]])

    rng = random.Random(42)
    emp_rows = []
    for i in range(800):
        did = rng.randint(1, 20)
        age = rng.randint(20, 65)
        region = rng.randint(1, 5)
        # correlation: band ≈ region (with rare noise)
        band = region if rng.random() > 0.05 else rng.randint(1, 5)
        salary = 30000 + region * 8000 + rng.randint(0, 2000)
        emp_rows.append(f"{i},{did},{age},{salary},{region},{band}")
    dept_rows = [f"{d},{1000*d},{d%3}" for d in range(1, 21)]
    proj_rows = [f"{p},{p%20+1},{rng.randint(100,900)}" for p in range(100)]
    asg_rows = [
        f"{rng.randint(0,799)},{rng.randint(0,99)},{rng.randint(1,40)}"
        for _ in range(1500)
    ]

    (OUT / "emp.csv").write_text("\n".join(emp_rows) + "\n", encoding="utf-8")
    (OUT / "dept.csv").write_text("\n".join(dept_rows) + "\n", encoding="utf-8")
    (OUT / "proj.csv").write_text("\n".join(proj_rows) + "\n", encoding="utf-8")
    (OUT / "assign.csv").write_text("\n".join(asg_rows) + "\n", encoding="utf-8")
    load_csv(cat, "Emp", OUT / "emp.csv", frames=32)
    load_csv(cat, "Dept", OUT / "dept.csv", frames=32)
    load_csv(cat, "Proj", OUT / "proj.csv", frames=32)
    load_csv(cat, "Assign", OUT / "assign.csv", frames=32)
    for t in ("Emp", "Dept", "Proj", "Assign"):
        analyze_table(cat, t, buckets=20, frames=32)

    queries = [
        "SELECT eid, age FROM Emp WHERE age > 40;",
        "SELECT eid FROM Emp WHERE did = 7;",
        "SELECT eid FROM Emp WHERE did = 7 AND age > 40;",
        "SELECT e.eid, d.budget FROM Emp e JOIN Dept d ON e.did = d.did;",
        "SELECT e.eid FROM Emp e, Dept d WHERE e.did = d.did AND d.budget > 5000;",
        "SELECT e.eid FROM Emp e JOIN Dept d ON e.did = d.did WHERE e.age > 50;",
        "SELECT did, COUNT(*) AS n FROM Emp GROUP BY did;",
        "SELECT did, AVG(salary) AS a FROM Emp GROUP BY did HAVING COUNT(*) > 5;",
        "SELECT DISTINCT did FROM Emp;",
        "SELECT e.eid FROM Emp e JOIN Assign a ON e.eid = a.eid;",
        "SELECT p.pid FROM Proj p JOIN Dept d ON p.did = d.did;",
        "SELECT e.eid FROM Emp e JOIN Assign a ON e.eid = a.eid JOIN Proj p ON a.pid = p.pid;",
        "SELECT e.eid FROM Emp e, Dept d, Proj p WHERE e.did = d.did AND d.did = p.did;",
        "SELECT eid FROM Emp WHERE salary > 50000;",
        "SELECT eid FROM Emp WHERE region = 3;",
        "SELECT eid FROM Emp WHERE band = 3;",
        # correlation breaker: region=3 AND band=3 looks independent but isn't
        "SELECT eid FROM Emp WHERE region = 3 AND band = 3;",
        "SELECT eid FROM Emp WHERE region = 3 AND band = 1;",
        "SELECT e.eid, d.city FROM Emp e JOIN Dept d ON e.did = d.did WHERE e.region = 2;",
        "SELECT COUNT(*) FROM Emp;",
    ]
    (OUT / "queries.json").write_text(json.dumps(queries, indent=2), encoding="utf-8")
    print("wrote", OUT, "queries", len(queries))


if __name__ == "__main__":
    main()
