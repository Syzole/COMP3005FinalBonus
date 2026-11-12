"""Run M1 buffer-policy hit rates for REPORT_M1 (§8 Policies)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.disk import DiskManager
from storage.buffer import BufferPool

TRACES = ROOT / "traces"
OUT = ROOT / "data" / "m1_policy_measurements.json"

# Five published traces (gen_traces.py). Exclude §5.3 worked example.
TRACE_FILES = [
    ("trace1", "trace1_random.txt"),
    ("trace2", "trace2_hot.txt"),
    ("trace3", "trace3_seq80x10.txt"),
    ("trace4", "trace4_loop16.txt"),
    ("trace5", "trace5_stride.txt"),
]
FRAMES = [4, 16, 64, 256]
POLICIES = ["lru", "clock"]


def run_trace(path: Path, frames: int, policy: str) -> dict:
    # Fresh backing file per run so disk counters stay clean.
    path_db = ROOT / "data" / f"_m1_trace_{policy}_{frames}.db"
    if path_db.exists():
        path_db.unlink()
    disk = DiskManager(path_db)
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(int(line))
    if ids:
        disk.ensure_pages(max(ids) + 1)
    pool = BufferPool(frames, policy, disk)
    for pid in ids:
        pool.fetch_page(pid)
        pool.unpin_page(pid, False)
    c = pool.stats()
    hits = c["hits"]
    misses = c["misses"]
    total = hits + misses
    hit_rate = (hits / total) if total else 0.0
    if path_db.exists():
        path_db.unlink()
    return {
        "hits": hits,
        "misses": misses,
        "evictions": c["evictions"],
        "hit_rate": hit_rate,
        "refs": len(ids),
    }


def main():
    # Ensure traces exist
    missing = [n for _, n in TRACE_FILES if not (TRACES / n).exists()]
    if missing:
        from scripts.gen_traces import main as gen

        gen()

    rows = []
    for label, fname in TRACE_FILES:
        path = TRACES / fname
        for policy in POLICIES:
            for B in FRAMES:
                r = run_trace(path, B, policy)
                rows.append(
                    {
                        "trace": label,
                        "file": fname,
                        "policy": policy,
                        "frames": B,
                        **r,
                    }
                )
                print(
                    f"{label:7} {policy:5} B={B:<3} hit_rate={r['hit_rate']:.4f} "
                    f"hits={r['hits']} misses={r['misses']}"
                )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
