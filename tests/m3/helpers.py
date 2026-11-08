from __future__ import annotations

import shutil
from pathlib import Path

from storage.catalog import Catalog
from storage.loader import load_csv
from sql.pipeline import compile_sql
from sql.errors import SqlError

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "db_m3_test"


def setup_catalog() -> Catalog:
    if DB.exists():
        shutil.rmtree(DB)
    DB.mkdir(parents=True)
    cat = Catalog(DB)
    cat.create_table(
        "Emp",
        [
            ["name", "VARCHAR", 20],
            ["age", "INT"],
            ["did", "INT"],
            ["salary", "INT"],
            ["mgr", "INT"],
            ["eid", "INT"],
            ["city", "VARCHAR", 20],
        ],
    )
    cat.create_table(
        "Dept",
        [
            ["did", "INT"],
            ["title", "VARCHAR", 20],
            ["budget", "INT"],
            ["city", "VARCHAR", 20],
        ],
    )
    cat.create_table("T3", [["x", "INT"], ["did", "INT"]])
    (ROOT / "data" / "_m3_emp.csv").write_text(
        "alice,35,1,50000,2,1,ottawa\nbob,25,1,40000,1,2,ottawa\ncarol,40,2,60000,1,3,toronto\n",
        encoding="utf-8",
    )
    (ROOT / "data" / "_m3_dept.csv").write_text(
        "1,eng,2000,ottawa\n2,hr,800,toronto\n", encoding="utf-8"
    )
    load_csv(cat, "Emp", ROOT / "data" / "_m3_emp.csv", frames=8)
    load_csv(cat, "Dept", ROOT / "data" / "_m3_dept.csv", frames=8)
    return cat


def expect_error(cat, query: str, category: str):
    try:
        compile_sql(query, cat)
        raise AssertionError(f"expected {category} error")
    except SqlError as e:
        assert e.category == category, f"got {e.category}: {e}"
        return e
