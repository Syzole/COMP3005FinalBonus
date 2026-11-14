"""Replay the Bonus 1 equality-join size sweep on the three M2 join operators.

Bonus 1 joined R(a, b) with S(b, c) on R.b = S.b, one match per key, and
counted every pair. This script loads the same shape (two INT columns, not
the wide M2 rows) and records wall time around execute_plan only.

Nested loop stops at 8,000 tuples. Hash and sort-merge continue through
64,000. A 1,000,000-tuple run is added only when the 64,000 hash join
finishes in a few seconds, and sort-merge is included there only if its
64,000 run is still quick.

Writes data/bonus1_join_measurements.json and
reports/figures/m2_bonus1_wall.svg.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exec.engine import execute_plan
from exec.ops import collect_data_pages
from storage.catalog import Catalog
from storage.disk import PAGE_SIZE, DiskManager
from storage.loader import parse_schema

# Wall seconds from COMP3005Bonus1/REPORT.md, nested loop, match rate 1.
BONUS1_SECONDS = {
    1000: 0.6196,
    2000: 2.5286,
    4000: 11.3385,
    8000: 44.1906,
    16000: 175.4136,
    32000: 703.4049,
    64000: 2664.9322,
}

BASE_SIZES = [1000, 2000, 4000, 8000]
LARGE_SIZES = [16000, 32000, 64000]
MILLION = 1_000_000
ALL_OPS = ("hash_join", "sort_merge_join", "nested_loop_join")
FAST_OPS = ("hash_join", "sort_merge_join")
# "A few seconds" for the 64k hash join, and "still quick" for sort-merge.
HASH_FEW_SECONDS = 15.0
SMJ_QUICK_SECONDS = 60.0
SEED = 1
OUT = ROOT / "data" / "bonus1_join_measurements.json"


def relation_rows(n: int, seed: int) -> list[list[int]]:
    """n rows [key, payload], keys 0..n-1, shuffled. One S row per R key."""
    rows = [[i, i] for i in range(n)]
    random.Random(seed).shuffle(rows)
    return rows


def load_pair(db: str, n: int) -> tuple[int, int]:
    spec_r = parse_schema("a:INT,b:INT")
    spec_s = parse_schema("b:INT,c:INT")
    cat = Catalog(db, frames=4, policy="lru")
    cat.create_table("R", spec_r)
    cat.create_table("S", spec_s)
    started = time.perf_counter()
    for name, schema, seed in (("R", spec_r, SEED), ("S", spec_s, SEED + 1)):
        heap, pool, _info = cat.open_heap(name, frames=4)
        rows = relation_rows(n, seed)
        step = 100_000 if n >= 100_000 else 0
        for i, row in enumerate(rows, start=1):
            heap.insert(row)
            if step and i % step == 0:
                print(f"    loaded {name} {i}/{n}", flush=True)
        pool.flush_all()
        if pool.pinned_count() != 0:
            raise RuntimeError(f"{name} left pinned frames")
    elapsed = time.perf_counter() - started
    pages_r = count_data_pages(Path(db) / "R.db")
    pages_s = count_data_pages(Path(db) / "S.db")
    print(
        f"  loaded n={n} pages R={pages_r} S={pages_s} in {elapsed:.1f}s",
        flush=True,
    )
    return pages_r, pages_s


def count_data_pages(path: Path) -> int:
    disk = DiskManager(path)
    return len(collect_data_pages(disk, bytearray(PAGE_SIZE)))


def join_plan(op: str) -> dict:
    return {
        "op": op,
        "condition": "R.b = S.b",
        "left": {"op": "seqscan", "table": "R"},
        "right": {"op": "seqscan", "table": "S"},
    }


def no_temps(db: str) -> None:
    tmp = Path(db) / "_m2tmp"
    if tmp.exists() and any(tmp.iterdir()):
        raise RuntimeError(f"temp files remain in {tmp}")


def run_join(db: str, n: int, op: str, frames: int, pages_r: int, pages_s: int, mode: str) -> dict:
    started = time.perf_counter()
    result = execute_plan(db, join_plan(op), frames)
    elapsed = time.perf_counter() - started
    no_temps(db)
    totals = result["totals"]
    out_n = len(result["rows"])
    if out_n != n:
        raise RuntimeError(f"{op} n={n} B={frames} returned {out_n} rows, expected {n}")
    if totals["tuples_out"] != n:
        raise RuntimeError(
            f"{op} n={n} tuples_out={totals['tuples_out']}, expected {n}"
        )
    root = result["root"]
    join_comparisons = root.counters()["comparisons"]
    parts = getattr(root, "partitions", None)
    # In-memory hash still fills `partitions` with mode "memory".
    partitioned = isinstance(parts, dict) and parts.get("mode") != "memory"
    if op == "nested_loop_join" and join_comparisons != n * n:
        raise RuntimeError(
            f"nested loop comparisons {join_comparisons} != {n * n}"
        )
    if mode == "grace" and not partitioned:
        raise RuntimeError(f"hash join n={n} B={frames} did not partition")
    row = {
        "n": n,
        "op": op,
        "mode": mode,
        "partitioned": partitioned,
        "B": frames,
        "pages_r": pages_r,
        "pages_s": pages_s,
        "comparisons": totals["comparisons"],
        "join_comparisons": join_comparisons,
        "page_reads": totals["page_reads"],
        "page_writes": totals["page_writes"],
        "output_tuples": out_n,
        "seconds": round(elapsed, 4),
        "bonus1_seconds": BONUS1_SECONDS.get(n),
    }
    print(
        f"  {op:20} {mode:7} B={frames:<5} "
        f"comps {join_comparisons:<12} reads {totals['page_reads']:<6} "
        f"writes {totals['page_writes']:<6} {elapsed:.4f}s",
        flush=True,
    )
    return row


def measure_size(n: int, ops: tuple[str, ...], grace: bool) -> list[dict]:
    db = tempfile.mkdtemp(prefix="b1join_")
    rows = []
    try:
        pages_r, pages_s = load_pair(db, n)
        # B-1 covers the build side, and B-2 covers the outer side, so hash
        # stays in memory and nested loop reads each side once.
        frames = max(pages_r, pages_s) + 2
        for op in ops:
            rows.append(run_join(db, n, op, frames, pages_r, pages_s, "memory"))
        if grace:
            rows.append(run_join(db, n, "hash_join", 16, pages_r, pages_s, "grace"))
    finally:
        shutil.rmtree(db, ignore_errors=True)
    return rows


def memory_row(rows: list[dict], n: int, op: str) -> dict | None:
    for row in rows:
        if row["n"] == n and row["op"] == op and row["mode"] == "memory":
            return row
    return None


def print_table(rows: list[dict]) -> None:
    print()
    print(
        f"{'n':>8}  {'op':<20}  {'mode':<7}  {'B':>5}  {'pages':>5}  "
        f"{'join comps':>12}  {'reads':>6}  {'writes':>6}  "
        f"{'seconds':>10}  {'bonus1 s':>10}"
    )
    for row in rows:
        pages = row["pages_r"]
        bonus = row["bonus1_seconds"]
        bonus_s = f"{bonus:.4f}" if bonus is not None else ""
        print(
            f"{row['n']:8}  {row['op']:<20}  {row['mode']:<7}  {row['B']:5}  "
            f"{pages:5}  {row['join_comparisons']:12}  {row['page_reads']:6}  "
            f"{row['page_writes']:6}  {row['seconds']:10.4f}  {bonus_s:>10}"
        )


def write_wall_svg(rows: list[dict], path: Path) -> None:
    """Log-scale wall time. Grace hash is omitted; it uses a different B."""
    width, height = 820, 480
    left, right, top, bottom = 72, 28, 78, 64
    plot_w = width - left - right
    plot_h = height - top - bottom
    sizes = [1000, 2000, 4000, 8000, 16000, 32000, 64000, 1_000_000]
    labels = ["1k", "2k", "4k", "8k", "16k", "32k", "64k", "1M"]
    series = [
        ("Bonus 1", [BONUS1_SECONDS.get(n) for n in sizes], "#a6192e"),
        (
            "nested loop",
            [memory_row(rows, n, "nested_loop_join") for n in sizes],
            "#b86e00",
        ),
        (
            "sort-merge",
            [memory_row(rows, n, "sort_merge_join") for n in sizes],
            "#1b4f72",
        ),
        ("hash", [memory_row(rows, n, "hash_join") for n in sizes], "#2e7d32"),
    ]
    drawn = []
    for name, raw, color in series:
        ys = []
        for item in raw:
            if item is None:
                ys.append(None)
            elif isinstance(item, dict):
                ys.append(item["seconds"])
            else:
                ys.append(item)
        drawn.append((name, ys, color))
    values = [v for _n, ys, _c in drawn for v in ys if v]
    lo, hi = min(values), max(values)

    def x_at(i: int) -> float:
        return left + plot_w * i / (len(sizes) - 1)

    def y_at(v: float) -> float:
        return top + plot_h * (1 - (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo)))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfaf8"/>',
        f'<text x="{left}" y="24" font-family="Georgia,serif" font-size="18">'
        "Wall time on the Bonus 1 join</text>",
    ]
    for i, (name, _ys, color) in enumerate(drawn):
        x = left + i * 180
        parts.append(
            f'<line x1="{x}" y1="46" x2="{x + 18}" y2="46" stroke="{color}" stroke-width="2.4"/>'
        )
        parts.append(
            f'<text x="{x + 24}" y="50" font-family="Georgia,serif" font-size="13">{name}</text>'
        )
    decade = 10 ** math.floor(math.log10(lo))
    while decade < hi:
        if lo < decade < hi:
            y = y_at(decade)
            label = f"{decade:g}"
            parts.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e4e0da"/>'
            )
            parts.append(
                f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
                f'font-family="Consolas,monospace" font-size="11" fill="#4a4642">{label}</text>'
            )
        decade *= 10
    parts.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#1b1a18"/>')
    parts.append(
        f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" y2="{top + plot_h}" stroke="#1b1a18"/>'
    )
    for i, label in enumerate(labels):
        parts.append(
            f'<text x="{x_at(i):.1f}" y="{top + plot_h + 20}" text-anchor="middle" '
            f'font-family="Consolas,monospace" font-size="12">{label}</text>'
        )
    parts.append(
        f'<text x="{width / 2:.0f}" y="{height - 12}" text-anchor="middle" '
        f'font-family="Georgia,serif" font-size="13">tuples on each side</text>'
    )
    mid = top + plot_h / 2
    parts.append(
        f'<text x="16" y="{mid:.0f}" text-anchor="middle" transform="rotate(-90 16 {mid:.0f})" '
        f'font-family="Georgia,serif" font-size="13">seconds, log scale</text>'
    )
    for _name, ys, color in drawn:
        run: list[tuple[int, float]] = []
        runs = []
        for i, v in enumerate(ys):
            if v is None:
                if run:
                    runs.append(run)
                    run = []
            else:
                run.append((i, v))
        if run:
            runs.append(run)
        for segment in runs:
            pts = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in segment)
            parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="2.4" points="{pts}"/>'
            )
            for i, v in segment:
                parts.append(
                    f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="3.5" fill="{color}"/>'
                )
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def main() -> None:
    rows: list[dict] = []
    for n in BASE_SIZES:
        print(f"size {n} ops={','.join(ALL_OPS)}", flush=True)
        rows.extend(measure_size(n, ALL_OPS, grace=False))
    for n in LARGE_SIZES:
        print(f"size {n} ops={','.join(FAST_OPS)}", flush=True)
        rows.extend(measure_size(n, FAST_OPS, grace=(n == 64000)))

    hash_64 = memory_row(rows, 64000, "hash_join")
    smj_64 = memory_row(rows, 64000, "sort_merge_join")
    million_ops: list[str] = []
    if hash_64 is not None and hash_64["seconds"] <= HASH_FEW_SECONDS:
        million_ops.append("hash_join")
        if smj_64 is not None and smj_64["seconds"] <= SMJ_QUICK_SECONDS:
            million_ops.append("sort_merge_join")
        print(
            f"size {MILLION} ops={','.join(million_ops)} "
            f"(64k hash {hash_64['seconds']:.4f}s)",
            flush=True,
        )
        rows.extend(measure_size(MILLION, tuple(million_ops), grace=False))
    else:
        taken = None if hash_64 is None else hash_64["seconds"]
        print(
            f"skip n={MILLION}: 64k hash took {taken}s "
            f"(threshold {HASH_FEW_SECONDS}s)",
            flush=True,
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "R(a INT, b INT), S(b INT, c INT)",
        "condition": "R.b = S.b",
        "match": 1,
        "seed": SEED,
        "bonus1_nested_loop_seconds": BONUS1_SECONDS,
        "note": (
            "seconds is execute_plan wall time only. "
            "comparisons sums every operator; join_comparisons is the join node. "
            "Bonus 1 held rows in Python lists; these runs read slotted pages."
        ),
        "rows": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    figure = ROOT / "reports" / "figures" / "m2_bonus1_wall.svg"
    write_wall_svg(rows, figure)
    print_table(rows)
    print(f"\nwrote {OUT}")
    print(f"wrote {figure}")


if __name__ == "__main__":
    main()
