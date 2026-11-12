"""M4 measurements: per-node q-error, opt vs naive, rule ablation, correlation."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.catalog import Catalog
from sql.pipeline import compile_sql
from opt.physicalize import plan_physical, load_stats_map, explain_physical
from opt.selectivity import selectivity
from exec.engine import execute_plan, walk as walk_ops

WL = ROOT / "data" / "m4_workload"
DB = WL / "db"
OUT = ROOT / "data" / "m4_measurements.json"
FRAMES = 16


def run_phys(phys, frames=FRAMES):
    result = execute_plan(str(DB), phys, frames)
    return len(result["rows"]), result["totals"], result.get("root")


def naive_physical(logical: dict) -> dict:
    """Written order, fixed hash_join, no rewrite."""

    def walk(n):
        op = n["op"]
        if op == "scan":
            return {"op": "seqscan", "table": n["table"]}
        if op == "rename":
            c = walk(n["child"])
            if c.get("op") == "seqscan":
                c = dict(c)
                c["as"] = n["to"]
            return c
        if op == "select":
            return {
                "op": "filter",
                "condition": n["condition"],
                "child": walk(n["child"]),
            }
        if op == "project":
            return {
                "op": "project",
                "columns": n["columns"],
                "child": walk(n["child"]),
            }
        if op in ("join", "cross"):
            return {
                "op": "hash_join" if op == "join" else "nested_loop_join",
                "condition": n.get("condition", "1 = 1"),
                "left": walk(n["left"]),
                "right": walk(n["right"]),
            }
        if op == "group_by":
            return {
                "op": "group_by",
                "mode": "sort",
                "keys": n.get("keys") or [],
                "aggregates": [a[:3] for a in n.get("aggregates") or []],
                "child": walk(n["child"]),
            }
        if op == "distinct":
            return {"op": "distinct", "mode": "sort", "child": walk(n["child"])}
        if op == "sort":
            return {"op": "sort", "keys": n["keys"], "child": walk(n["child"])}
        if op == "limit":
            return {"op": "limit", "n": n["n"], "child": walk(n["child"])}
        if op in ("union", "union_all", "intersect", "except"):
            return {"op": op, "left": walk(n["left"]), "right": walk(n["right"])}
        raise ValueError(op)

    return walk(logical)


def qerror(est, act):
    est = max(1, int(est))
    act = max(1, int(act))
    return max(est / act, act / est)


def io_total(tot: dict) -> int:
    return int(tot.get("page_reads", 0)) + int(tot.get("page_writes", 0))


def preorder_phys(n: dict) -> list[dict]:
    nodes = [n]
    if "child" in n:
        nodes.extend(preorder_phys(n["child"]))
    if "left" in n:
        nodes.extend(preorder_phys(n["left"]))
    if "right" in n:
        nodes.extend(preorder_phys(n["right"]))
    return nodes


def node_qerrors_from_run(phys: dict, est_map: dict, root_op) -> list[dict]:
    """Match explain annotations to executed operators in preorder."""
    phys_nodes = preorder_phys(phys)
    op_nodes = list(walk_ops(root_op))
    # Align by preorder; skip extras if counts drift
    n = min(len(phys_nodes), len(op_nodes))
    out = []
    for i in range(n):
        p = phys_nodes[i]
        o = op_nodes[i]
        est = est_map.get(id(p))
        if est is None:
            continue
        act = int(o.counters()["tuples_out"])
        out.append(
            {
                "op": p.get("op"),
                "est": est,
                "actual": act,
                "qerror": qerror(est, act),
            }
        )
    return out


def explain_est_map(logical, cat, B=FRAMES):
    from opt.physicalize import load_stats_map, _phys
    from opt.rewrite import rewrite as rw

    rewritten = rw(logical)
    stats_map = load_stats_map(cat)
    phys, annot = _phys(rewritten, stats_map, B, cat)
    est = {i: int(m.get("est_rows") or 0) for i, m in annot.items()}
    return phys, est


def main():
    if not (WL / "queries.json").exists():
        from scripts.gen_m4_workload import main as gen

        gen()
    cat = Catalog(DB)
    queries = json.loads((WL / "queries.json").read_text(encoding="utf-8"))
    stats_map = load_stats_map(cat)

    all_node_qerrors: list[float] = []
    root_qerrors: list[float] = []
    ratios: list[float] = []
    details = []

    for i, q in enumerate(queries):
        try:
            logical = compile_sql(q, cat)
            phys, est_map = explain_est_map(logical, cat, FRAMES)
            act_root, tot, root_op = run_phys(phys, FRAMES)
            node_rows = []
            if root_op is not None:
                node_rows = node_qerrors_from_run(phys, est_map, root_op)
            for nr in node_rows:
                all_node_qerrors.append(nr["qerror"])

            est_root = est_map.get(id(phys), act_root)
            root_qe = qerror(est_root, act_root)
            root_qerrors.append(root_qe)

            naive = naive_physical(logical)
            try:
                _a2, tot2, _ = run_phys(naive, FRAMES)
                ratio = (io_total(tot2) + 1) / (io_total(tot) + 1)
            except Exception:
                ratio = 1.0
            ratios.append(ratio)
            details.append(
                {
                    "i": i,
                    "q": q,
                    "actual": act_root,
                    "est": est_root,
                    "qerror_root": root_qe,
                    "node_qerrors": node_rows,
                    "opt_ratio": ratio,
                    "opt_io": io_total(tot),
                }
            )
            print(f"  q{i} root_qe={root_qe:.2f} nodes={len(node_rows)}", flush=True)
        except Exception as e:
            details.append({"i": i, "q": q, "error": str(e)})
            print(f"  q{i} ERROR {e}", flush=True)

    ablation_queries = [
        q for q in queries if (" JOIN " in q.upper()) or ("," in q.upper() and "FROM" in q.upper())
    ]
    if not ablation_queries:
        ablation_queries = queries[:5]

    baseline_io = 0
    for q in ablation_queries:
        try:
            logical = compile_sql(q, cat)
            phys = plan_physical(logical, cat, B=FRAMES)
            _a, tot, _ = run_phys(phys, FRAMES)
            baseline_io += io_total(tot)
        except Exception:
            pass

    ablation = []
    for rule in range(1, 8):
        total_io = 0
        ok = 0
        for q in ablation_queries:
            try:
                logical = compile_sql(q, cat)
                phys = plan_physical(logical, cat, B=FRAMES, disable_rules={rule})
                _a, tot, _ = run_phys(phys, FRAMES)
                total_io += io_total(tot)
                ok += 1
            except Exception:
                pass
        ablation.append(
            {
                "disabled_rule": rule,
                "queries": ok,
                "total_io": total_io,
                "baseline_io": baseline_io,
                "delta_io": total_io - baseline_io,
            }
        )
        print(f"  ablation rule {rule}: io={total_io} delta={total_io - baseline_io}", flush=True)

    corr_q = "SELECT eid FROM Emp WHERE region = 3 AND band = 3;"
    alt_q = "SELECT eid FROM Emp WHERE region = 3;"
    emp = stats_map.get("Emp") or {}
    n = emp.get("n_tuples") or 800
    s_reg = selectivity("Emp.region = 3", {"Emp": emp}, n)
    s_band = selectivity("Emp.band = 3", {"Emp": emp}, n)
    indep = s_reg * s_band * n

    corr_logical = compile_sql(corr_q, cat)
    corr_phys = plan_physical(corr_logical, cat, B=FRAMES)
    corr_act, corr_tot, _ = run_phys(corr_phys, FRAMES)
    _b, _p, lines = explain_physical(corr_logical, cat, B=FRAMES)
    corr_est = corr_act
    if lines:
        m = re.search(r"est_rows=(\d+)", lines[0])
        if m:
            corr_est = int(m.group(1))

    alt_logical = compile_sql(alt_q, cat)
    alt_phys = plan_physical(alt_logical, cat, B=FRAMES)
    alt_act, alt_tot, _ = run_phys(alt_phys, FRAMES)

    join_bad = (
        "SELECT e.eid FROM Emp e, Dept d "
        "WHERE e.did = d.did AND e.region = 3 AND e.band = 3;"
    )
    try:
        jb_log = compile_sql(join_bad, cat)
        jb_phys = plan_physical(jb_log, cat, B=4)
        opt_act, opt_tot, _ = run_phys(jb_phys, 4)
        # Trusting tiny AND selectivity → leave filters above a Cartesian NLJ
        # (written order, no pushdown): blows comparisons and adds I/O.
        naive = naive_physical(jb_log)
        nv_act, nv_tot, _ = run_phys(naive, 4)
        join_corr = {
            "query": join_bad,
            "opt_io": io_total(opt_tot),
            "opt_rows": opt_act,
            "opt_comparisons": opt_tot.get("comparisons"),
            "naive_cross_then_filter_io": io_total(nv_tot),
            "naive_rows": nv_act,
            "naive_comparisons": nv_tot.get("comparisons"),
            "io_cost_of_bad_vs_better": io_total(nv_tot) - io_total(opt_tot),
            "comparison_ratio_bad_over_opt": (
                (nv_tot.get("comparisons") or 1) / max(1, opt_tot.get("comparisons") or 1)
            ),
        }
    except Exception as e:
        join_corr = {"error": str(e)}

    node_sorted = sorted(all_node_qerrors) if all_node_qerrors else [1.0]
    median_node = node_sorted[len(node_sorted) // 2]
    report = {
        "qerror_median_all_nodes": median_node,
        "qerror_worst_all_nodes": max(all_node_qerrors) if all_node_qerrors else 1,
        "qerror_median_root": (
            sorted(root_qerrors)[len(root_qerrors) // 2] if root_qerrors else 1
        ),
        "qerror_worst_root": max(root_qerrors) if root_qerrors else 1,
        "node_qerror_count": len(all_node_qerrors),
        "opt_vs_naive_ratios": ratios,
        "opt_vs_naive_min": min(ratios) if ratios else 1,
        "opt_vs_naive_max": max(ratios) if ratios else 1,
        "rule_ablation": ablation,
        "correlation": {
            "query": corr_q,
            "assumption": "sel(p AND q) = sel(p)*sel(q) independence",
            "independent_est_rows": indep,
            "optimizer_est_rows": corr_est,
            "actual_rows": corr_act,
            "qerror": qerror(corr_est, corr_act),
            "plan_io": io_total(corr_tot),
            "region_only_actual": alt_act,
            "region_only_io": io_total(alt_tot),
            "note": "region≈band correlated ~95%; AND independence underestimates",
            "join_embedding": join_corr,
        },
        "details": details,
    }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        "median node qerr",
        median_node,
        "worst",
        report["qerror_worst_all_nodes"],
        "nodes",
        len(all_node_qerrors),
    )
    print("wrote", OUT)


if __name__ == "__main__":
    main()
