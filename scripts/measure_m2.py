"""Measure Module 2 join cost, sort passes, crossover, skew, and the frame budget.

Writes data/m2_measurements.json and the two crossover plots under reports/figures/.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exec.engine import execute_plan
from storage.catalog import Catalog
from storage.disk import PAGE_SIZE
from storage.loader import parse_schema
from storage.page import NO_SPACE, SlottedPage, encode_tuple

WIDTH = 200
SPEC_B = [4, 16, 64, 256]
SPEC_PAGES = [8, 24, 48]
# Extra points so the crossover plots show where the ranking moves.
SWEEP_B = [4, 8, 16, 32, 64, 128, 256]
SWEEP_PAGES = 24
SIZE_B = 4
SIZE_PAGES = [8, 16, 24, 36, 48]
SKEW_PAGES = 20
SKEW_B = [4, 16]
SORT_CASES = [(8, 3), (10, 4), (16, 4), (24, 4), (24, 8), (48, 16), (64, 16), (80, 4)]
JOIN_OPS = ("nested_loop_join", "sort_merge_join", "hash_join")


def tuples_per_page(schema, row) -> int:
    page = SlottedPage.fresh(bytearray(PAGE_SIZE), 0)
    rec = encode_tuple(schema, row)
    n = 0
    while page.insert(rec) != NO_SPACE:
        n += 1
    return n


def sort_io(n_pages: int, frames: int) -> tuple[int, int]:
    """Carry-forward sort from section 2.5. Final pass reads and does not write.

    Reads include the original input pages. Returns (reads, writes).
    """
    if n_pages <= 0:
        return 0, 0
    reads = n_pages
    writes = 0
    runs: list[int] = []
    left = n_pages
    while left:
        take = min(frames, left)
        left -= take
        last = left == 0
        if not runs and last:
            return reads, writes
        writes += take
        runs.append(take)
    fan = frames - 1
    while len(runs) > 1:
        if len(runs) <= fan:
            reads += sum(runs)
            return reads, writes
        new: list[int] = []
        i = 0
        while i < len(runs):
            group = runs[i : i + fan]
            i += fan
            if len(group) == 1:
                new.append(group[0])
            else:
                pages = sum(group)
                reads += pages
                writes += pages
                new.append(pages)
        runs = new
    return reads, writes


def pass_count(n_pages: int, frames: int) -> int:
    if n_pages <= 0:
        return 0
    runs = math.ceil(n_pages / frames)
    if runs <= 1:
        return 1
    k = 0
    covered = 1
    fan = frames - 1
    while covered < runs:
        covered *= fan
        k += 1
    return 1 + k


def log_passes(n_pages: int, frames: int) -> int:
    runs = math.ceil(n_pages / frames)
    if runs <= 1:
        return 1
    return 1 + math.ceil(math.log(runs) / math.log(frames - 1) - 1e-9)


def nlj_formula(m: int, n: int, frames: int) -> tuple[int, int]:
    blocks = math.ceil(m / (frames - 2))
    return m + blocks * n, 0


def smj_formula(m: int, n: int, frames: int) -> tuple[int, int]:
    lr, lw = sort_io(m, frames)
    rr, rw = sort_io(n, frames)
    return lr + rr + m + n, lw + rw


def hash_formula(m: int, n: int) -> tuple[int, int]:
    return 2 * (m + n), m + n


def load_pair(db: str, pages: int, per: int, kind: str) -> int:
    spec = f"k:INT,v:VARCHAR({WIDTH})"
    schema = parse_schema(spec)
    n = pages * per
    if kind == "uniform":
        rows = [[i, "x" * WIDTH] for i in range(n)]
        hot = 0
    elif kind == "skew":
        hot = (n * 2) // 5
        rows = [[0, "x" * WIDTH] for _ in range(hot)]
        rows += [[i + 1, "x" * WIDTH] for i in range(n - hot)]
    else:
        raise ValueError(kind)
    cat = Catalog(db, frames=4, policy="lru")
    for name in ("R", "S"):
        cat.create_table(name, parse_schema(spec))
        heap, pool, _info = cat.open_heap(name, frames=4)
        for row in rows:
            heap.insert(row)
        pool.flush_all()
        if pool.pinned_count() != 0:
            raise RuntimeError(f"{name} left pinned frames")
    return hot


def load_sort_table(db: str, pages: int, per: int) -> None:
    spec = f"a:INT,b:VARCHAR({WIDTH})"
    cat = Catalog(db, frames=4, policy="lru")
    cat.create_table("R", parse_schema(spec))
    heap, pool, _info = cat.open_heap("R", frames=4)
    for i in range(pages * per):
        heap.insert([i, "x" * WIDTH])
    pool.flush_all()
    if pool.pinned_count() != 0:
        raise RuntimeError("sort table left pinned frames")


def join_plan(op: str) -> dict:
    return {
        "op": op,
        "condition": "R.k = S.k",
        "left": {"op": "seqscan", "table": "R"},
        "right": {"op": "seqscan", "table": "S"},
    }


def walk_tree(op) -> dict:
    counted = op.counters()
    node = {
        "op": op.trace_label(),
        "reads": counted["page_reads"],
        "writes": counted["page_writes"],
        "tuples_out": counted["tuples_out"],
        "comparisons": counted["comparisons"],
    }
    if getattr(op, "partitions", None) is not None:
        node["partitions"] = op.partitions
        node["overflow"] = op.overflow
    node["children"] = [walk_tree(child) for child in op.children]
    return node


def no_temps(db: str) -> None:
    tmp = Path(db) / "_m2tmp"
    if tmp.exists() and any(tmp.iterdir()):
        raise RuntimeError(f"temp files remain in {tmp}")


def run_join(db: str, op: str, frames: int, pages: int, kind: str) -> dict:
    started = time.perf_counter()
    result = execute_plan(db, join_plan(op), frames)
    elapsed = time.perf_counter() - started
    no_temps(db)
    root = result["root"]
    scans = {child.table: child.page_reads for child in root.children}
    totals = result["totals"]
    # The inner scan of a nested loop is reopened once per outer block, so its
    # counter is the repeated reads, not the size of S. Both relations were
    # loaded as `pages` full data pages.
    if scans["R"] != pages:
        raise RuntimeError(f"R scan read {scans['R']} pages, expected {pages}")
    m = pages
    n = pages
    if op == "nested_loop_join":
        fr, fw = nlj_formula(m, n, frames)
    elif op == "sort_merge_join":
        fr, fw = smj_formula(m, n, frames)
    else:
        fr, fw = hash_formula(m, n)
    row = {
        "op": op,
        "kind": kind,
        "pages": pages,
        "B": frames,
        "M": m,
        "N": n,
        "formula_reads": fr,
        "formula_writes": fw,
        "measured_reads": totals["page_reads"],
        "measured_writes": totals["page_writes"],
        "comparisons": totals["comparisons"],
        "tuples_out": totals["tuples_out"],
        "peak": result["budget"].peak,
        "seconds": round(elapsed, 3),
        "tree": walk_tree(root),
    }
    print(
        f"  {op:20} B={frames:<3} pages={pages:<3} {kind:8} "
        f"reads {totals['page_reads']}/{fr} writes {totals['page_writes']}/{fw} "
        f"peak {result['budget'].peak} {elapsed:.1f}s",
        flush=True,
    )
    return row, result["rows"]


def run_sort(db: str, pages: int, frames: int, per: int) -> dict:
    plan = {
        "op": "sort",
        "keys": [["R.a", "asc"]],
        "child": {"op": "seqscan", "table": "R"},
    }
    started = time.perf_counter()
    result = execute_plan(db, plan, frames)
    elapsed = time.perf_counter() - started
    no_temps(db)
    root = result["root"]
    keys = [row[0] for row in result["rows"]]
    expect = list(range(pages * per))
    measured = root.passes
    formula = log_passes(root.input_pages, frames)
    integer = pass_count(root.input_pages, frames)
    row = {
        "N": root.input_pages,
        "B": frames,
        "measured_passes": measured,
        "formula_passes": formula,
        "integer_passes": integer,
        "page_reads": result["totals"]["page_reads"],
        "page_writes": result["totals"]["page_writes"],
        "peak": result["budget"].peak,
        "held_after": result["budget"].held,
        "sorted": keys == expect,
        "tuples": len(keys),
        "seconds": round(elapsed, 3),
        "is_20x": pages == 20 * frames,
    }
    print(
        f"  sort N={root.input_pages:<3} B={frames:<3} passes {measured} "
        f"formula {formula} peak {result['budget'].peak} {elapsed:.1f}s",
        flush=True,
    )
    return row


def jobs_for(pages: int) -> list[int]:
    frames = set()
    if pages in SPEC_PAGES:
        frames.update(SPEC_B)
    if pages == SWEEP_PAGES:
        frames.update(SWEEP_B)
    if pages in SIZE_PAGES:
        frames.add(SIZE_B)
    return sorted(frames)


def measure_joins(per: int) -> tuple[list[dict], dict]:
    page_counts = sorted(set(SPEC_PAGES + SIZE_PAGES + [SWEEP_PAGES]))
    rows = []
    bags = {}
    for pages in page_counts:
        frames_list = jobs_for(pages)
        db = tempfile.mkdtemp(prefix="m2meas_")
        try:
            print(f"loading uniform {pages} pages", flush=True)
            load_pair(db, pages, per, "uniform")
            for frames in frames_list:
                got = {}
                for op in JOIN_OPS:
                    row, out_rows = run_join(db, op, frames, pages, "uniform")
                    rows.append(row)
                    got[op] = Counter(tuple(r) for r in out_rows)
                same = got["nested_loop_join"] == got["sort_merge_join"] == got["hash_join"]
                bags[f"{pages}:{frames}"] = same
                if not same:
                    print(f"  BAG MISMATCH pages={pages} B={frames}", flush=True)
        finally:
            shutil.rmtree(db, ignore_errors=True)
    return rows, bags


def measure_skew(per: int) -> list[dict]:
    rows = []
    n = SKEW_PAGES * per
    for kind in ("uniform", "skew"):
        db = tempfile.mkdtemp(prefix="m2skew_")
        try:
            print(f"loading {kind} {SKEW_PAGES} pages", flush=True)
            hot = load_pair(db, SKEW_PAGES, per, kind)
            for frames in SKEW_B:
                row, _rows = run_join(db, "hash_join", frames, SKEW_PAGES, kind)
                row["hot_rows"] = hot
                row["rows_per_side"] = n
                row["hot_fraction"] = round(hot / n, 4) if n else 0
                rows.append(row)
        finally:
            shutil.rmtree(db, ignore_errors=True)
    return rows


def measure_sorts(per: int) -> list[dict]:
    rows = []
    by_pages: dict[int, str] = {}
    try:
        for pages, frames in SORT_CASES:
            if pages not in by_pages:
                db = tempfile.mkdtemp(prefix="m2sort_")
                print(f"loading sort table {pages} pages", flush=True)
                load_sort_table(db, pages, per)
                by_pages[pages] = db
            rows.append(run_sort(by_pages[pages], pages, frames, per))
    finally:
        for db in by_pages.values():
            shutil.rmtree(db, ignore_errors=True)
    return rows


def write_svg(path: Path, title: str, xlabel: str, xs: list, series: list[tuple[str, list[int]]]) -> None:
    width, height = 760, 460
    left, right, top, bottom = 78, 36, 78, 64
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [v for _name, ys in series for v in ys if v > 0]
    lo = min(values)
    hi = max(values)
    if hi == lo:
        hi = lo * 2

    def x_at(i: int) -> float:
        if len(xs) == 1:
            return left + plot_w / 2
        return left + plot_w * i / (len(xs) - 1)

    def y_at(v: int) -> float:
        return top + plot_h * (1 - (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo)))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfaf8"/>',
        f'<text x="{left}" y="24" font-family="Georgia,serif" font-size="18">{title}</text>',
    ]
    colors = ["#a6192e", "#1b4f72", "#2e7d32"]
    for i, ((name, _ys), color) in enumerate(zip(series, colors)):
        x = left + i * 170
        parts.append(f'<line x1="{x}" y1="46" x2="{x + 18}" y2="46" stroke="{color}" stroke-width="2.4"/>')
        parts.append(
            f'<text x="{x + 24}" y="50" font-family="Georgia,serif" font-size="13">{name}</text>'
        )
    for tick in (lo, hi):
        y = y_at(tick)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e4e0da"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="Consolas,monospace" font-size="11" fill="#4a4642">{tick}</text>'
        )
    parts.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#1b1a18"/>'
    )
    parts.append(
        f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" y2="{top + plot_h}" stroke="#1b1a18"/>'
    )
    for i, label in enumerate(xs):
        x = x_at(i)
        parts.append(
            f'<text x="{x:.1f}" y="{top + plot_h + 20}" text-anchor="middle" '
            f'font-family="Consolas,monospace" font-size="12">{label}</text>'
        )
    parts.append(
        f'<text x="{width / 2:.0f}" y="{height - 12}" text-anchor="middle" '
        f'font-family="Georgia,serif" font-size="13">{xlabel}</text>'
    )
    mid = top + plot_h / 2
    parts.append(
        f'<text x="16" y="{mid:.0f}" text-anchor="middle" transform="rotate(-90 16 {mid:.0f})" '
        f'font-family="Georgia,serif" font-size="13">reads + writes, log scale</text>'
    )
    for (name, ys), color in zip(series, colors):
        pts = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(ys))
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.4" points="{pts}"/>')
        for i, v in enumerate(ys):
            parts.append(f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="3.5" fill="{color}"/>')
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def series_for(rows: list[dict], pages: int | None, frames: int | None, key: str) -> list[tuple[str, list]]:
    labels = {
        "nested_loop_join": "block nested loop",
        "sort_merge_join": "sort-merge",
        "hash_join": "hash",
    }
    xs = []
    chosen = [r for r in rows if r["kind"] == "uniform"]
    if pages is not None:
        chosen = [r for r in chosen if r["pages"] == pages]
        xs = sorted({r["B"] for r in chosen})
        pick = "B"
    else:
        chosen = [r for r in chosen if r["B"] == frames]
        xs = sorted({r["pages"] for r in chosen})
        pick = "pages"
    out = []
    for op in JOIN_OPS:
        ys = []
        for x in xs:
            match = next(r for r in chosen if r["op"] == op and r[pick] == x)
            ys.append(match["measured_reads"] + match["measured_writes"])
        out.append((labels[op], ys))
    return xs, out


def cheapest(rows: list[dict], pages: int | None, frames: int | None) -> list[dict]:
    chosen = [r for r in rows if r["kind"] == "uniform"]
    if pages is not None:
        chosen = [r for r in chosen if r["pages"] == pages]
        axis = "B"
        values = sorted({r["B"] for r in chosen})
    else:
        chosen = [r for r in chosen if r["B"] == frames]
        axis = "pages"
        values = sorted({r["pages"] for r in chosen})
    ranking = []
    for value in values:
        group = [r for r in chosen if r[axis] == value]
        group.sort(key=lambda r: (r["measured_reads"] + r["measured_writes"], r["op"]))
        ranking.append(
            {
                axis: value,
                "order": [
                    {
                        "op": r["op"],
                        "cost": r["measured_reads"] + r["measured_writes"],
                        "reads": r["measured_reads"],
                        "writes": r["measured_writes"],
                    }
                    for r in group
                ],
            }
        )
    return ranking


def main() -> None:
    sample_schema = parse_schema(f"k:INT,v:VARCHAR({WIDTH})")
    per = tuples_per_page(sample_schema, [0, "x" * WIDTH])
    check_r, check_w = sort_io(8, 3)
    if (check_r, check_w) != (22, 14):
        raise SystemExit(f"sort formula failed the worked example: {(check_r, check_w)}")
    print(f"tuples per page = {per}", flush=True)
    joins, bags = measure_joins(per)
    skew = measure_skew(per)
    sorts = measure_sorts(per)
    figures = ROOT / "reports" / "figures"
    b_xs, b_series = series_for(joins, SWEEP_PAGES, None, "B")
    s_xs, s_series = series_for(joins, None, SIZE_B, "pages")
    write_svg(
        figures / "m2_crossover_b.svg",
        f"Join I/O as B grows ({SWEEP_PAGES} pages each side)",
        "B (frames)",
        b_xs,
        b_series,
    )
    write_svg(
        figures / "m2_crossover_size.svg",
        f"Join I/O as the relations grow (B = {SIZE_B})",
        "pages on each side",
        s_xs,
        s_series,
    )
    payload = {
        "width": WIDTH,
        "tuples_per_page": per,
        "record_bytes": 4 + 2 + WIDTH,
        "cost": "page reads + page writes",
        "sort_formula": "section 2.5 carry-forward; final pass does not write; sort(M)+sort(N)+M+N",
        "hash_formula": "reads 2(M+N), writes M+N, total 3(M+N)",
        "nlj_formula": "reads M + ceil(M/(B-2))*N, writes 0",
        "joins": joins,
        "same_bag": bags,
        "skew": skew,
        "sorts": sorts,
        "crossover_b": cheapest(joins, SWEEP_PAGES, None),
        "crossover_size": cheapest(joins, None, SIZE_B),
    }
    out = ROOT / "data" / "m2_measurements.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
