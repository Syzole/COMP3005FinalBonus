"""Page I/O cost model matching M2."""

from __future__ import annotations

import math

def sort_passes(M: int, B: int) -> int:
    """External-sort pass count for M pages, B frames."""
    if M <= 0:
        return 0
    if M <= B:
        return 1  # fits in memory
    # 1 + ceil(log_{B-1}(ceil(M/B)))
    return 1 + math.ceil(math.log(math.ceil(M / B)) / math.log(max(2, B - 1)))


def cost_sort(M: int, B: int) -> float:
    """Sort I/O: 2M(passes-1) + M."""
    if M <= 0:
        return 0
    p = sort_passes(M, B)
    return 2 * M * (p - 1) + M


def cost_seqscan(M: int) -> float:
    """Read all M pages once."""
    return float(M)


def cost_nlj(M: int, N: int, B: int) -> float:
    """BNL: M + ceil(M/(B-2))*N."""
    return M + math.ceil(M / max(1, B - 2)) * N


def cost_smj(M: int, N: int, B: int) -> float:
    """Sort both sides, then merge scan."""
    return cost_sort(M, B) + cost_sort(N, B) + M + N


def cost_hash(M: int, N: int) -> float:
    """Grace hash: 3(M+N)."""
    return 3 * (M + N)


def choose_join(M: int, N: int, B: int, equality: bool = True) -> tuple[str, float]:
    """Return (physical_op, cost). Inequality → nested_loop only."""
    if not equality:
        return "nested_loop_join", cost_nlj(M, N, B)
    # pick cheapest among NLJ / SMJ / hash
    cands = [
        ("nested_loop_join", cost_nlj(M, N, B)),
        ("sort_merge_join", cost_smj(M, N, B)),
        ("hash_join", cost_hash(M, N)),
    ]
    return min(cands, key=lambda x: x[1])
