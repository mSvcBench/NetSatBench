#!/usr/bin/env python3
"""
OVERVIEW
--------
This scheduler replaces the flat best-fit logic with a two-level METIS-based
placement strategy that is aware of both the dynamic satellite topology and
the compute resources of each worker host.

PHASE 1  —  build_epoch_weights()
    Streams all epoch JSON files once.
    For every undirected edge (node_i, node_j) it counts the number of epochs
    the link was active  →  edge_active_count[(i,j)]
    For every node it counts the total number of active links across all epochs
    →  node_activity[node_name]
    These two dictionaries become the edge / node weights for METIS.

PHASE 2  —  analyse_requirements()
    Reads CPU / MEM requests for every node and every worker.
    Computes:
      • total demand vs. total supply (logs a warning on overcommit)
      • per-worker max_nodes  (min of cpu-slots, mem-slots, len(nodes)/num_workers)
      • METIS integer node-weight = resource_weight + activity_weight
        so that heavy AND topologically central nodes are kept together.

PHASE 3  —  hierarchical_metis_schedule()
    PRE-ASSIGNED nodes  (node config has 'worker' field pointing to a valid worker)
        → kept on their assigned worker, their resources deducted immediately.
          They still participate in METIS so their edges affect the partition.

    ZERO-RESOURCE nodes  (cpu-request == 0  AND  mem-request == 0)
        → treated as "privileged": placed on the worker with the most free
          resources (highest score) BEFORE the METIS partition is applied,
          since they can consume an unbounded amount of resources at runtime.

    ALL other nodes  →  two-level pymetis:
        L1: partition all schedulable nodes into k = num_workers spatial groups
            using combined (epoch-count + activity) edge/node weights.
            Pre-assigned nodes are included in the graph so their links pull
            their neighbours toward the correct worker, but they are not
            reassigned.
        L2: for each spatial group check if it fits on its preferred worker.
            If not  →  sub-partition recursively with resource-weighted METIS
            until every piece fits or max_depth is reached.


Parameters
----------
config_data : dict
    Merged sat-config (output of merge_node_common_config).
alpha : float
    Multiplier that scales edge_active_count into METIS edge weights.
    Higher  →  topology locality matters more.
beta : float
    Multiplier that scales node_activity into the METIS node weight addend.
    Higher  →  busy nodes attract their neighbours more strongly.
"""

import argparse
import json
import math
import os
import re
import sys
import logging
from collections import defaultdict
from glob import glob
from typing import Any, Dict, List, Optional, Set, Tuple

import pymetis  # noqa: PLC0415

# Single logger for the entire module — never re-declared below.
log = logging.getLogger("nsb-scheduler")


# ──────────────────────────────────────────────────────────────────────────────
# UNIT HELPERS  (identical to the rest of the codebase)
# ──────────────────────────────────────────────────────────────────────────────

def _parse_cpu(value) -> float:
    if not value:
        return 0.0
    val = str(value).strip()
    if val.endswith("m"):
        try:
            return float(val[:-1]) / 1000.0
        except ValueError:
            return 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0


def _parse_mem(value) -> float:
    if not value:
        return 0.0
    val = str(value).strip()
    units = {
        "Ti": 1024.0,   "Gi": 1.0,   "Mi": 1.0 / 1024.0,    "Ki": 1.0 / 1_048_576.0,
        "TiB": 1024.0,  "GiB": 1.0,  "MiB": 1.0 / 1024.0,   "KiB": 1.0 / 1_048_576.0,
        "T": 1024.0,    "G": 1.0,    "M": 1.0 / 1024.0,      "K": 1.0 / 1_048_576.0,
    }
    m = re.match(r"([0-9.]+)([a-zA-Z]+)?", val)
    if not m:
        return 0.0
    try:
        num = float(m.group(1))
        unit = m.group(2)
        return num * units[unit] if unit and unit in units else num
    except ValueError:
        return 0.0


def _get_prefix_data(etcd, prefix: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for val, meta in etcd.get_prefix(prefix):
        key = meta.key.decode().split("/")[-1]
        try:
            out[key] = json.loads(val.decode())
        except json.JSONDecodeError:
            log.warning(f"⚠️  Bad JSON for etcd key {key}")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# EPOCH FILE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _list_epoch_files(epoch_dir: str, file_pattern: str) -> List[str]:
    if not epoch_dir or not file_pattern:
        return []
    search_path = os.path.join(epoch_dir, file_pattern)

    def _last_num(p: str) -> int:
        nums = re.findall(r"(\d+)", os.path.basename(p))
        return int(nums[-1]) if nums else -1

    return sorted(glob(search_path), key=_last_num)


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.warning(f"⚠️  JSON error in {path}: {e}  — skipping file")
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 1  —  build_epoch_weights
# ──────────────────────────────────────────────────────────────────────────────

def build_epoch_weights(
    config_data:  dict,
    epoch_dir:    str = "",
    file_pattern: str = "",
) -> Tuple[Dict[Tuple[int, int], int], Dict[str, int]]:
    """
    Stream all epoch JSON files and return:
        edge_active_count  {(min_idx, max_idx) -> n_epochs_active}
        node_activity      {node_name          -> total_active_link_epochs}

    """
    nodes: Dict[str, Any] = config_data.get("nodes", {})
    epoch_cfg = config_data.get("epoch-config", {})
    ep_dir = epoch_dir  or epoch_cfg.get("epoch-dir",    "")
    ep_pat = file_pattern or epoch_cfg.get("file-pattern", "")

    # Forward map  name → global index
    node_map: Dict[str, int] = {n: i for i, n in enumerate(nodes)}
    # Reverse map  global index → name  (built once; O(1) lookups in hot loop)
    inv_node_map: Dict[int, str] = {i: n for n, i in node_map.items()}

    files = _list_epoch_files(ep_dir, ep_pat)

    edge_cnt: Dict[Tuple[int, int], int] = defaultdict(int)
    node_act: Dict[str, int]             = defaultdict(int)
    active:   Set[Tuple[int, int]]       = set()   # currently-live edges

    if not files:
        log.warning(
            "⚠️  No epoch files found — METIS will use resource-only weights. "
            f"(epoch_dir={ep_dir!r}, pattern={ep_pat!r})"
        )
        return {}, {}

    for path in files:
        ep = _load_json(path)

        # Apply links-add
        for lnk in ep.get("links-add", []):
            s, d = lnk.get("endpoint1"), lnk.get("endpoint2")
            if s not in node_map or d not in node_map or s == d:
                continue
            i, j = node_map[s], node_map[d]
            edge_key = (i, j) if i < j else (j, i)
            active.add(edge_key)

        # Apply links-del
        for lnk in ep.get("links-del", []):
            s, d = lnk.get("endpoint1"), lnk.get("endpoint2")
            if s not in node_map or d not in node_map or s == d:
                continue
            i, j = node_map[s], node_map[d]
            edge_key = (i, j) if i < j else (j, i)
            active.discard(edge_key)

        # Accumulate counts for this epoch snapshot
        node_link_count: Dict[str, int] = defaultdict(int)
        for (i, j) in active:
            edge_cnt[(i, j)] += 1
            # O(1) reverse lookup via pre-built inv_node_map
            node_link_count[inv_node_map[i]] += 1
            node_link_count[inv_node_map[j]] += 1
        for name, cnt in node_link_count.items():
            node_act[name] += cnt

    log.info(
        f"📡 Epoch weights built: {len(files)} files, "
        f"{len(edge_cnt)} edges with activity, "
        f"{len(node_act)} nodes with activity"
    )
    return dict(edge_cnt), dict(node_act)


# ──────────────────────────────────────────────────────────────────────────────
# ADAPTIVE ALPHA / BETA  —  derive weights from statistical signal strength
# ──────────────────────────────────────────────────────────────────────────────

def _cv(values: List[float]) -> float:
    """
    Coefficient of Variation = std_dev / mean.

    Returns 0.0 for empty or constant distributions (no signal to exploit).
    CV measures *relative* spread:
    a high CV means the distribution is skewed and it is worth paying attention to its outliers;
    a low CV means values are uniform and weighting them heavily adds no information.
    """
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0.0:
        return 0.0
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance) / mean


def auto_alpha_beta(
    edge_active_count: Dict[Tuple[int, int], int],
    all_nodes:         Dict[str, Any],
    common_cfg:        Dict[str, Any],
    node_activity:     Dict[str, int],
) -> Tuple[float, float, int]:
    """
    Derive alpha and beta from the statistical properties of the input data
    and scale them to a METIS-meaningful integer range [1, 100].

    Strategy
    --------
    alpha  (edge topology weight)
    ─────────────────────────────
        base_alpha = median(edge_weights) / max(edge_weights)   ∈ (0, 1]
        alpha_norm = min(base_alpha * (1 + cv_edges), 1.0)      CV boost, capped at 1
        alpha      = max(1, alpha_norm * 100)                   scale to [1, 100]

        High cv_edges → links have very different lifetimes → alpha near 100.
        Low cv_edges  → all links equally persistent        → alpha near 1.

    beta  (node activity weight)
    ────────────────────────────
        base_beta  = median(node_activity) / max(node_activity)  ∈ (0, 1]
        beta_norm  = min(base_beta * (1 + cv_joint), 1.0)        CV boost, capped at 1
        beta       = max(1, beta_norm * 100)                     scale to [1, 100]

        cv_joint = mean(cv_cpu, cv_mem, cv_activity)
        High heterogeneity → busy/heavy nodes matter more → beta near 100.
        Low heterogeneity  → all nodes similar            → beta near 1.

    max_depth
    ─────────
        Each recursion level can at minimum halve the group, so
        ceil(log₂(n/k)) levels are enough to reach single nodes.
        +1 safety margin, floored at 2 so tiny graphs still split.

    Parameters
    ----------
    edge_active_count : output of build_epoch_weights
    all_nodes         : config_data['nodes']
    common_cfg        : config_data['node-config-common']
    node_activity     : output of build_epoch_weights

    Returns
    -------
    (alpha, beta, max_depth) — alpha/beta rounded to 2 decimal places
    """
    def _percentile(values: List[float], p: float) -> float:
        """Return the p-th percentile (0–100) of a sorted list."""
        if not values:
            return 1.0
        sv = sorted(values)
        idx = (p / 100.0) * (len(sv) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(sv) - 1)
        return sv[lo] + (sv[hi] - sv[lo]) * (idx - lo)

    def _median(values: List[float]) -> float:
        return _percentile(values, 50)

    # ── alpha: base and ceiling from edge weight distribution ─────────────────
    ''' base_alpha is normalised by max(edge_weights) so it lives in [0, 1].
    This puts alpha and beta on the same scale, preventing one from
    dominating the METIS node-weight formula purely due to unit differences.
   
      node_weight = round(alpha x res_score) + round(beta x act_score)
   
    Without normalisation median(edge)=2 vs median(activity)=20 would make
    beta ~10x heavier than alpha regardless of actual signal strength.'''
    
    edge_weights = [float(v) for v in edge_active_count.values()] or [1.0]
    cv_edges     = _cv(edge_weights)
    max_edge     = max(edge_weights)

    # ── alpha ────────────────────────────────────────────────────────────────────
    ''' base_alpha = median(edge_weights) / max(edge_weights)  ∈ (0, 1]
    alpha_norm = min(base_alpha *(1 + cv_edges), 1.0)   
    alpha      = max(1, alpha_norm * 100)                scale to [1, 100]
    High cv_edges → links have different lifetimes → alpha near 100.
    Low cv_edges  → all links equally persistent  → alpha near 1.'''
    
    edge_weights = [float(v) for v in edge_active_count.values()] or [1.0]
    cv_edges     = _cv(edge_weights)
    max_edge     = max(edge_weights)

    base_alpha = _median(edge_weights) / max_edge          # ∈ (0, 1]
    alpha_norm = min(base_alpha * (1.0 + cv_edges), 1.0)  # CV boost, capped at 1
    alpha      = round(max(1.0, alpha_norm * 100), 2)     # scale to [1, 100]

    # ── beta ─────────────────────────────────────────────────────────────────
    ''' base_beta = median(node_activity) / max(node_activity)  ∈ (0, 1]
    cv_joint = mean(cv_cpu, cv_mem, cv_activity)
    beta = max(1, min(base_beta * (1 + cv_joint), 1.0) * 100)  →  [1, 100]'''
    
    default_cpu = _parse_cpu(common_cfg.get("cpu-request", "0"))
    default_mem = _parse_mem(common_cfg.get("mem-request", "0"))

    cpus = [
        _parse_cpu(cfg.get("cpu-request")) or default_cpu
        for cfg in all_nodes.values()
    ]
    mems = [
        _parse_mem(cfg.get("mem-request")) or default_mem
        for cfg in all_nodes.values()
    ]
    activities   = [float(v) for v in node_activity.values()] or [1.0]
    max_activity = max(activities)

    cv_cpu       = _cv(cpus)
    cv_mem       = _cv(mems)
    cv_activity  = _cv(activities)
    cv_joint     = (cv_cpu + cv_mem + cv_activity) / 3.0

    base_beta = _median(activities) / max_activity         # ∈ (0, 1]
    beta_norm = min(base_beta * (1.0 + cv_joint), 1.0)    # CV boost, capped at 1
    beta      = round(max(1.0, beta_norm * 100), 2)       # scale to [1, 100]

    # ── max_depth: derived from graph size ────────────────────────────────────
    ''' n_workers from pre-assigned nodes is often 0 or 1 and skews the estimate.
     Heuristic: sqrt(n_nodes) gives a reasonable partition count for most graphs.
     ceil(log2(avg_group)) + 1 levels suffice to halve groups down to singles. '''
    
    n_nodes   = len(all_nodes)
    n_workers  = max(2, round(math.sqrt(n_nodes)))
    avg_group  = max(2, n_nodes / n_workers)
    max_depth  = max(2, math.ceil(math.log2(avg_group)) + 1)

    log.info(
        f"📊 Auto alpha/beta/max_depth:"
        f"  cv_edges={cv_edges:.3f}  base_alpha={base_alpha:.3f}"
        f"  alpha_norm={alpha_norm:.3f} → alpha={alpha}"
        f"  |  cv_cpu={cv_cpu:.3f}  cv_mem={cv_mem:.3f}  cv_activity={cv_activity:.3f}"
        f"  base_beta={base_beta:.3f}  beta_norm={beta_norm:.3f} → beta={beta}"
        f"  |  n_nodes={n_nodes}  max_depth={max_depth}"
    )
    return alpha, beta, max_depth


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 2  —  analyse_requirements
# ──────────────────────────────────────────────────────────────────────────────

def analyse_requirements(
    all_nodes:         Dict[str, Any],
    common_cfg:        Dict[str, Any],
    workers:           Dict[str, Any],
    edge_active_count: Dict[Tuple[int, int], int],
    node_activity:     Dict[str, int],
    alpha:             float,
    beta:              float,
) -> Tuple[
    Dict[str, float],   # node_cpu          (effective — avg substituted for zero-resource)
    Dict[str, float],   # node_mem          (effective — avg substituted for zero-resource)
    Dict[str, int],     # node_weight       (METIS integer)
    Dict[str, int],     # worker_max_nodes
]:
    """
    Returns per-node effective CPU/MEM, METIS vertex weights, and per-worker
    maximum node capacity.

    Effective resource for zero-resource nodes
    ──────────────────────────────────────────
    A node with cpu=0 AND mem=0 is a container that can consume ALL shared
    resources on its worker at runtime.  For worker capacity accounting we
    substitute the average resource of all other nodes (excluding zero-resource
    nodes themselves to avoid circular averaging):

        effective_cpu(n) = mean cpu  of nodes where cpu > 0  OR  mem > 0
        effective_mem(n) = mean mem  of nodes where cpu > 0  OR  mem > 0

    This effective value is used ONLY for deducting from worker free capacity.
    METIS vertex weights are computed separately (see below).

    METIS node weight formula
    ─────────────────────────
        w(n) = max(1,
                   round(alpha * resource_score(n))
                 + round(beta  * activity_score(n))
               )

    where
        resource_score(n) = cpu(n)/max_cpu + mem(n)/max_mem   ∈ [0, 2]
        activity_score(n) = node_activity(n) / max_activity   ∈ [0, 1]

    Uses RAW cpu/mem (zero stays zero) for the weight formula.
    Zero-resource nodes keep cpu=0/mem=0 here — their METIS weight strategy
    is decided independently of capacity accounting.

    Pinned-no-resource nodes (worker declared, cpu=0, mem=0) receive
    max_node_weight so METIS treats them as the heaviest node in the graph,
    maximising the pull of their edges toward their pinned worker.
    """
    default_cpu = _parse_cpu(common_cfg.get("cpu-request", "0"))
    default_mem = _parse_mem(common_cfg.get("mem-request", "0"))

    # ── Raw resource values from config ──────────────────────────────────────
    node_cpu_raw: Dict[str, float] = {}
    node_mem_raw: Dict[str, float] = {}
    for name, cfg in all_nodes.items():
        node_cpu_raw[name] = _parse_cpu(cfg.get("cpu-request")) or default_cpu
        node_mem_raw[name] = _parse_mem(cfg.get("mem-request")) or default_mem

    # ── Identify zero-resource nodes ──────────────────────────────────────────
    zero_resource_nodes: Set[str] = {
        n for n in all_nodes
        if node_cpu_raw[n] == 0.0 and node_mem_raw[n] == 0.0
    }

    # ── Average resource — from all nodes EXCEPT zero-resource ones ───────────
    """ Used as effective capacity for zero-resource nodes so that their
     worker slot deduction reflects a realistic (conservative) estimate
     rather than zero."""
     
    other_nodes = [n for n in all_nodes if n not in zero_resource_nodes]
    if other_nodes:
        avg_cpu = sum(node_cpu_raw[n] for n in other_nodes) / len(other_nodes)
        avg_mem = sum(node_mem_raw[n] for n in other_nodes) / len(other_nodes)
    else:
        avg_cpu = 0.0
        avg_mem = 0.0

    if zero_resource_nodes:
        log.info(
            f"   Zero-resource nodes ({len(zero_resource_nodes)}): "
            f"effective_cpu={avg_cpu:.4f}  effective_mem={avg_mem:.4f} GiB  "
            f"(avg of {len(other_nodes)} non-zero nodes — used for capacity accounting only)"
        )

    # ── Effective resource per node ───────────────────────────────────────────
    """ node_cpu / node_mem are the values used for worker capacity deduction
     and the _fits_group / _best_worker checks in Phase 3.
     Zero-resource nodes get avg substituted; all others keep their raw value."""
    node_cpu: Dict[str, float] = {}
    node_mem: Dict[str, float] = {}
    for name in all_nodes:
        if name in zero_resource_nodes:
            node_cpu[name] = avg_cpu   # effective = average of non-zero nodes
            node_mem[name] = avg_mem
        else:
            node_cpu[name] = node_cpu_raw[name]
            node_mem[name] = node_mem_raw[name]

    # ── Demand vs supply (uses effective values) ──────────────────────────────
    total_cpu_demand = sum(node_cpu.values())
    total_mem_demand = sum(node_mem.values())
    total_cpu_supply = sum(_parse_cpu(w.get("cpu", 0)) for w in workers.values())
    total_mem_supply = sum(_parse_mem(w.get("mem", 0)) for w in workers.values())

    log.info("🔍 Resource Analysis:")
    log.info(f"   Nodes: {len(all_nodes)}  "
             f"(zero-resource: {len(zero_resource_nodes)}, using avg for capacity)")
    log.info(
        f"   CPU  demand={total_cpu_demand:.2f}  supply={total_cpu_supply:.2f}  "
        f"{'✅' if total_cpu_supply >= total_cpu_demand else '⚠️  OVERCOMMIT'}"
    )
    log.info(
        f"   MEM  demand={total_mem_demand:.2f} GiB  supply={total_mem_supply:.2f} GiB  "
        f"{'✅' if total_mem_supply >= total_mem_demand else '⚠️  OVERCOMMIT'}"
    )

    # ── per-worker max_nodes ──────────────────────────────────────────────────
    # Use only nodes that are not pre-assigned to compute the "average" node size.
    
    schedulable = [n for n, c in all_nodes.items() if "worker" not in c] #n= node name, c= node config
    N_sched = len(schedulable) or 1  # N = number of schedulable nodes; avoid div by zero
    avg_cpu_slot = sum(node_cpu[n] for n in schedulable) / N_sched
    avg_mem_slot = sum(node_mem[n] for n in schedulable) / N_sched

    worker_max_nodes: Dict[str, int] = {}
    log.info("   Workers:")
    for wname, wcfg in workers.items():
        wcpu = _parse_cpu(wcfg.get("cpu", 0))
        wmem = _parse_mem(wcfg.get("mem", 0))

        by_cpu = int(wcpu / avg_cpu_slot) if avg_cpu_slot > 0 else len(all_nodes)
        by_mem = int(wmem / avg_mem_slot) if avg_mem_slot > 0 else len(all_nodes)

        mn = max(1, min(by_cpu + by_mem // 2, len(all_nodes)))
        worker_max_nodes[wname] = mn
        log.info(
            f"     {wname}: cpu={wcpu:.2f}  mem={wmem:.2f} GiB  "
            f"max_nodes={mn}  (cpu_slots={by_cpu} mem_slots={by_mem})"
        )

    # ── METIS integer node weights ────────────────────────────────────────────
    """ Uses RAW values (node_cpu_raw / node_mem_raw), not effective values,
     so that zero-resource nodes keep weight derived from cpu=0/mem=0.
     Pinned-no-resource nodes are patched to max_node_weight in second pass.
     
     Identify nodes that have a pinned worker but NO resource declaration.
     They receive the maximum vertex weight in METIS.
     This achieves two things:
       (a) their neighbours are pulled toward the pinned worker as strongly
           as possible (maximising topology locality), and
       (b) METIS balances other partitions *around* them rather than treating
           them as cheap, movable nodes.
     """
    max_cpu = max(node_cpu_raw.values(), default=1.0) or 1.0
    max_mem = max(node_mem_raw.values(), default=1.0) or 1.0
    max_act = max(node_activity.values(), default=1) or 1

    pinned_no_resource: Set[str] = {
        name for name, cfg in all_nodes.items()
        if cfg.get("worker")           # has a pinned worker
        and node_cpu_raw[name] == 0.0  # no CPU declared
        and node_mem_raw[name] == 0.0  # no MEM declared
    }

    # First pass: compute weights for nodes with declared resources.
    node_weight: Dict[str, int] = {}
    for name in all_nodes:
        if name in pinned_no_resource:
            continue                          # patched in second pass
        res_score = node_cpu_raw[name] / max_cpu + node_mem_raw[name] / max_mem  # [0,2]
        act_score = node_activity.get(name, 0) / max_act                         # [0,1]
        node_weight[name] = max(
            1,
            round(alpha * res_score) + round(beta * act_score),
        )

    # Second pass: assign max_node_weight to pinned-no-resource nodes.
    # Computed AFTER the first pass so we know the true ceiling of the graph.
    computed_max = max(node_weight.values(), default=1)
    for name in pinned_no_resource:
        node_weight[name] = computed_max
        log.info(
            f"  🔴 Pinned+no-resource: {name} → "
            f"weight={computed_max} (max_node_weight)  "
            f"[capacity deducted as avg_cpu={avg_cpu:.4f} avg_mem={avg_mem:.4f} GiB]"
        )

    return node_cpu, node_mem, node_weight, worker_max_nodes


# ──────────────────────────────────────────────────────────────────────────────
# METIS CSR HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _build_csr(
    node_indices: List[int],
    edge_weight:  Dict[Tuple[int, int], int],
    node_weight:  Dict[int, int],
) -> Tuple[List[int], List[int], List[int], List[int]]:
    """Build pymetis CSR arrays for the subgraph induced by node_indices."""
    local = {g: l for l, g in enumerate(node_indices)}
    n = len(node_indices)
    adj: List[List[Tuple[int, int]]] = [[] for _ in range(n)]

    for (a, b), w in edge_weight.items():
        if a in local and b in local and w > 0:
            la, lb = local[a], local[b]
            adj[la].append((lb, int(w)))
            adj[lb].append((la, int(w)))

    for row in adj:
        row.sort()

    xadj, adjncy, ew = [0], [], []
    for row in adj:
        for v, w in row:
            adjncy.append(v)
            ew.append(w)
        xadj.append(len(adjncy))

    vw = [node_weight.get(node_indices[i], 1) for i in range(n)]
    return xadj, adjncy, ew, vw


def _pymetis_partition(
    node_indices: List[int],
    edge_weight:  Dict[Tuple[int, int], int],
    node_weight:  Dict[int, int],
    nparts:       int,
) -> List[int]:
    """Partition node_indices into nparts; returns part list aligned to node_indices.

    Uses the xadj/adjncy CSR interface so that both edge weights (eweights) and
    vertex weights (vweights) can be supplied.  CSRAdjacency is NOT used because
    it does not exist in current pymetis releases — xadj and adjncy are passed
    directly as keyword arguments to part_graph().
    """
    if nparts <= 1 or len(node_indices) <= nparts:
        return [i % nparts for i in range(len(node_indices))]

    xadj, adjncy, ew, vw = _build_csr(node_indices, edge_weight, node_weight)
    if not adjncy:
        # Disconnected graph — fall back to round-robin
        return [i % nparts for i in range(len(node_indices))]

    adjacency = pymetis.CSRAdjacency(xadj, adjncy)
    _, parts = pymetis.part_graph(
        nparts,
        adjacency=adjacency,
        eweights=ew,
        vweights=vw,
    )
    return list(map(int, parts))


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 3  —  hierarchical_metis_schedule  (core)
# ──────────────────────────────────────────────────────────────────────────────

def hierarchical_metis_schedule(
    config_data:       Dict[str, Any],
    edge_active_count: Dict[Tuple[int, int], int],
    node_activity:     Dict[str, int],
    workers:           Dict[str, Any],
    alpha:             float,
    beta:              float,
    max_depth:         int,
) -> Dict[str, Any]:
    """
    Mutates config_data['nodes'] in place: adds / updates the 'worker' field
    for every node.  Returns config_data for convenience.

    Node categories
    ───────────────
    PRE-ASSIGNED   node has 'worker' field pointing to a valid worker
                   → resources deducted immediately; node enters METIS as an
                     anchor so its edges bias neighbours toward the right worker.
                   → After L1, each partition's preferred worker is steered
                     toward whichever pinned worker has the most anchors in
                     that partition (topology + pinning, priority 1).

    ZERO-RESOURCE  cpu==0 AND mem==0, no pinned worker
                   → enters METIS as anchor (edges still influence neighbours).
                   → After L1, placed on the partition's anchor-steered worker
                     if it has headroom (topology wins), else richest worker.

    SCHEDULABLE    everything else  →  L1 + L2 METIS placement, using the
                   anchor-steered preferred worker for each L1 group.
    """
    all_nodes  = config_data.get("nodes", {})
    common_cfg = config_data.get("node-config-common", {})

    if not all_nodes:
        log.error("❌ No nodes in config.")
        sys.exit(1)
    if not workers:
        log.error("❌ No workers available.")
        sys.exit(1)

    # ── Phase 2: requirements ─────────────────────────────────────────────────
    node_cpu, node_mem, node_weight_map, worker_max_nodes = analyse_requirements(
        all_nodes, common_cfg, workers,
        edge_active_count, node_activity,
        alpha=alpha, beta=beta,
    )

    # ── Combined edge weight (epoch count scaled by alpha) ────────────────────
    combined_ew: Dict[Tuple[int, int], int] = {
        edge_key: max(1, round(v * alpha))
        for edge_key, v in edge_active_count.items()
    }

    # ── Index helpers ─────────────────────────────────────────────────────────
    node_map: Dict[str, int] = {n: i for i, n in enumerate(all_nodes)}
    inv_map:  Dict[int, str] = {i: n for n, i in node_map.items()}
    nw_idx:   Dict[int, int] = {
        node_map[n]: w for n, w in node_weight_map.items() if n in node_map
    }

    # ── Worker state table ────────────────────────────────────────────────────
    worker_list = sorted(workers.keys())
    k = len(worker_list)

    worker_res: Dict[str, Dict] = {}
    for wn in worker_list:
        wc  = workers[wn]
        cpu = _parse_cpu(wc.get("cpu", 0))
        mem = _parse_mem(wc.get("mem", 0))
        worker_res[wn] = {
            "cpu":       cpu,
            "mem":       mem,
            "cpu-used":  _parse_cpu(wc.get("cpu-used", 0)),
            "mem-used":  _parse_mem(wc.get("mem-used", 0)),
            "max-nodes": worker_max_nodes[wn],
            "assigned":  [],
            "data":      wc,
        }

    # ── Helper closures ───────────────────────────────────────────────────────
    def _free_cpu(wn: str) -> float:
        return worker_res[wn]["cpu"] - worker_res[wn]["cpu-used"]

    def _free_mem(wn: str) -> float:
        return worker_res[wn]["mem"] - worker_res[wn]["mem-used"]

    # slots — practical OS limit, not just resources
    def _free_slots(wn: str) -> int:
        return worker_res[wn]["max-nodes"] - len(worker_res[wn]["assigned"])

    def _score(wn: str) -> float:
        # Normalised score: sum of free-fraction for each resource axis.
        wr = worker_res[wn]
        cpu_frac = _free_cpu(wn) / max(wr["cpu"], 1e-9)
        mem_frac = _free_mem(wn) / max(wr["mem"], 1e-9)
        return cpu_frac + mem_frac

    def _assign(name: str, wn: str) -> None:
        all_nodes[name]["worker"] = wn
        w = worker_res[wn]
        # node_cpu/node_mem already hold effective values (avg for zero-resource)
        w["cpu-used"] += node_cpu[name] or 1e-9
        w["mem-used"] += node_mem[name] or 1e-9
        w["assigned"].append(name)

    def _fits_group(wn: str, group: List[str]) -> bool:
        return (
            _free_cpu(wn)   >= sum(node_cpu[n] for n in group)
            and _free_mem(wn)   >= sum(node_mem[n] for n in group)
            and _free_slots(wn) >= len(group)
        )

    def _best_worker(name: str) -> Optional[str]:
        # Tier 1: worker has free slots AND enough cpu/mem
        cands = [
            wn for wn in worker_list
            if _free_slots(wn) > 0
            and _free_cpu(wn) >= node_cpu[name]
            and _free_mem(wn) >= node_mem[name]
        ]
        if cands:
            return max(cands, key=_score)
        # Tier 2: worker has free slots but resource overcommit
        cands2 = [wn for wn in worker_list if _free_slots(wn) > 0]
        if cands2:
            log.warning(f"  ⚠️  Resource-overcommitting {name}")
            return max(cands2, key=_score)
        # Tier 3: all workers are at max-nodes — slot overcommit on richest
        log.warning(f"  ⚠️  Slot-overcommitting {name} (all workers full)")
        return max(worker_list, key=_score)

    # ── PRE-ASSIGNED nodes ────────────────────────────────────────────────────
    """ Deduct resources immediately. Node stays in the METIS graph as an anchor
     (edges bias free neighbours toward the right worker); partition result
     for this node is discarded after L1.
     node_cpu already holds effective values, so zero-resource pre-assigned
     nodes deduct avg_cpu/avg_mem from their worker instead of zero."""
     
    pre_assigned: Set[str] = set()
    pre_assigned_worker: Dict[str, str] = {}

    for name, ncfg in list(all_nodes.items()):
        aw = ncfg.get("worker")
        if aw and aw in workers:
            pre_assigned.add(name)
            pre_assigned_worker[name] = aw
            worker_res[aw]["cpu-used"] += node_cpu[name]   # effective value
            worker_res[aw]["mem-used"] += node_mem[name]   # effective value
            worker_res[aw]["assigned"].append(name)
            log.info(f"  📌 Pre-assigned (anchor): {name} → {aw}  "
                     "[edges visible to METIS]")
        elif aw:
            log.warning(
                f"  ⚠️  Node {name} has worker='{aw}' not in workers list "
                "→ will be rescheduled."
            )
            del ncfg["worker"]

    # ── ZERO-RESOURCE nodes ───────────────────────────────────────────────────
    """ cpu==0 AND mem==0, no pinned worker.
     Kept as anchors in the METIS graph so their topology edges influence
     neighbour placement.  After L1 we use the METIS partition result to
     steer them toward their topologically correct worker, falling back to
     the richest worker only when that worker lacks headroom.
     We identify them from the raw config to avoid false positives when
     avg_cpu/avg_mem happen to equal a real node's declared resources."""
     
    default_cpu = _parse_cpu(common_cfg.get("cpu-request", "0"))
    default_mem = _parse_mem(common_cfg.get("mem-request", "0"))
    zero_res_nodes: Set[str] = {
        n for n in all_nodes
        if n not in pre_assigned
        and (_parse_cpu(all_nodes[n].get("cpu-request")) or default_cpu) == 0.0
        and (_parse_mem(all_nodes[n].get("mem-request")) or default_mem) == 0.0
    }

    if zero_res_nodes:
        log.info(
            f"   Zero-resource nodes ({len(zero_res_nodes)}): "
            "kept as anchors in METIS graph; "
            "placement: topology-preferred worker if headroom available, "
            "else richest worker."
        )

    # ── Schedulable nodes ─────────────────────────────────────────────────────
    needs_sched: List[str] = [
        n for n in all_nodes if n not in pre_assigned and n not in zero_res_nodes
    ]

    if not needs_sched:
        log.info("✅ All nodes pre-assigned or zero-resource; nothing left for METIS.")
        for name in zero_res_nodes:
            wn = max(worker_list, key=_score)
            _assign(name, wn)
            log.info(f"    ➞ {name} → {wn}  (zero-resource, richest worker)")
        return config_data

    # ── Recursive L2 scheduler ────────────────────────────────────────────────
    def _schedule_group(group: List[str], preferred: str, depth: int) -> None:
        if not group:
            return
        pad = "    " * (depth + 1)

        if _fits_group(preferred, group):
            for n in group:
                _assign(n, preferred)
            log.info(f"{pad}✅ {len(group)} nodes → {preferred}")
            return

        if depth >= max_depth:
            log.warning(f"{pad}⚠️  max_depth reached; individual best-fit fallback")
            for n in group:
                wn = _best_worker(n)
                if not wn:
                    log.error(f"❌ Cannot schedule {n}")
                    sys.exit(1)
                _assign(n, wn)
            return

        g_cpu  = sum(node_cpu[n] for n in group)
        g_mem  = sum(node_mem[n] for n in group)
        g_size = len(group)

        # n_sub: use cluster-wide free capacity, not just the preferred worker.
        # Using only preferred worker underestimates n_sub when it is nearly full.
        # free_cluster_* = sum across ALL workers that still have open slots.
        avail = [wn for wn in worker_list if _free_slots(wn) > 0]

        free_cluster_cpu   = sum(_free_cpu(wn)   for wn in avail) or 1e-9
        free_cluster_mem   = sum(_free_mem(wn)   for wn in avail) or 1e-9
        free_cluster_slots = sum(_free_slots(wn) for wn in avail) or 1

        n_sub = max(2, math.ceil(max(
            g_cpu  / free_cluster_cpu,
            g_mem  / free_cluster_mem,
            g_size / free_cluster_slots,
        )))
        n_sub = min(n_sub, len(avail))

        if n_sub < 2:
            for n in group:
                wn = _best_worker(n)
                if not wn:
                    log.error(f"❌ Cannot schedule {n}")
                    sys.exit(1)
                _assign(n, wn)
            return

        log.info(f"{pad}🔀 depth={depth}: {len(group)} nodes → {n_sub} sub-groups")

        g_idx = [node_map[n] for n in group if n in node_map]
        parts = _pymetis_partition(g_idx, combined_ew, nw_idx, n_sub)

        subs: Dict[int, List[str]] = defaultdict(list)
        # Map partition result back to node names; parts are aligned to g_idx which is aligned to group.
        for li, part in enumerate(parts):
            subs[part].append(inv_map[g_idx[li]])

        # Sort available workers by score and assign sub-groups in order of size,
        # giving preference to the original preferred worker.
        avail_sorted = sorted(avail, key=_score, reverse=True)
        used_wn: Set[str] = {preferred}
        for sub_id, sub_nodes in sorted(subs.items(), key=lambda x: -len(x[1])):
            chosen = next(
                (wn for wn in avail_sorted if wn not in used_wn),
                preferred,
            )
            used_wn.add(chosen)
            log.info(f"{pad}  Sub-{sub_id}: {len(sub_nodes)} nodes → {chosen}")
            _schedule_group(sub_nodes, chosen, depth + 1)

    # ── LEVEL 1: full graph partition (anchors included) ──────────────────────
    """ All nodes enter the partition so anchor edges bias the result.
    After partitioning:
       • pre-assigned anchors  → their partition-id is used to steer the
                                 preferred worker for that L1 group toward
                                 the pinned worker with the most anchors
                                 (priority 1: pinning + topology locality).
       • zero-resource anchors → placed on the anchor-steered preferred worker
                                 if it has headroom (topology wins), else
                                 richest worker (safety fallback).
       • schedulable nodes     → extracted into l1_groups; L2 uses the same
                                 anchor-steered preferred worker."""
    log.info(
        f"🔵 L1-METIS: {len(needs_sched)} schedulable + "
        f"{len(pre_assigned) + len(zero_res_nodes)} anchor nodes → "
        f"{k} spatial groups"
    )

    all_metis_idx:   List[int] = [node_map[n] for n in all_nodes if n in node_map]
    all_metis_names: List[str] = [inv_map[i] for i in all_metis_idx]

    l1_parts_full = _pymetis_partition(all_metis_idx, combined_ew, nw_idx, k)

    # Build name → partition-id lookup used by both anchor-steering and
    # zero-resource topology placement below.
    node_to_part: Dict[str, int] = {
        name: part for name, part in zip(all_metis_names, l1_parts_full)
    }

    # ── Dual-objective preferred worker per L1 partition ─────────────────────
    """ For each partition we pick the worker that maximises:
    
       partition_score(worker, partition) =
           w1 * anchor_affinity(worker, partition)   ← topology signal
         + w2 * norm_free_score(worker)              ← resource signal
    
     Weights w1 / w2 are derived from the CV of the input data so that
     whichever signal has higher variance (= more discriminating power)
     gets proportionally more influence:
    
       cv_resources = mean(cv_cpu, cv_mem)   from analyse_requirements
       w1 = cv_edges     / (cv_edges + cv_resources)   topology weight
       w2 = cv_resources / (cv_edges + cv_resources)   resource weight
    
     anchor_affinity(worker, partition) =
       votes(worker, partition) / max(total_anchors_in_partition, 1)  ∈ [0,1]
    
     norm_free_score(worker) = _score(worker) / 2.0  ∈ [0,1]
       (_score returns cpu_frac + mem_frac, each ∈ [0,1], so max = 2)
    
     This means:
      • A partition with many anchors on one worker → topology wins → that worker
      • A partition with no anchors → resource wins → richest worker
      • Mixed cases → continuous blend decided by CV ratio from data

    # ── Compute w1 / w2 from CV of edges and resources ────────────────────────
     Re-use the same CV values that auto_alpha_beta computed; we recalculate
     locally so this function stays self-contained."""
    
    _default_cpu = _parse_cpu(common_cfg.get("cpu-request", "0"))
    _default_mem = _parse_mem(common_cfg.get("mem-request", "0"))
    _cpus = [_parse_cpu(cfg.get("cpu-request")) or _default_cpu
             for cfg in all_nodes.values()]
    _mems = [_parse_mem(cfg.get("mem-request")) or _default_mem
             for cfg in all_nodes.values()]

    def _cv_local(vals: List[float]) -> float:
        if len(vals) < 2:
            return 0.0
        mean = sum(vals) / len(vals)
        if mean == 0.0:
            return 0.0
        var = sum((x - mean) ** 2 for x in vals) / len(vals)
        return math.sqrt(var) / mean

    cv_edges_local     = _cv_local([float(v) for v in edge_active_count.values()] or [0.0])
    cv_resources_local = (_cv_local(_cpus) + _cv_local(_mems)) / 2.0
    _denom = cv_edges_local + cv_resources_local
    if _denom == 0.0:
        # No signal in either dimension — equal weights
        w1, w2 = 0.5, 0.5
    else:
        w1 = cv_edges_local     / _denom   # topology weight
        w2 = cv_resources_local / _denom   # resource weight

    # ── Cluster-relative capacity score for partition selection ─────────────
    # _score(wn) = free_cpu/own_cpu + free_mem/own_mem  is *relative* to each
    # worker's own capacity — a tiny worker and a large worker both score 2.0
    # when fully empty.  For deciding which worker should *host* a partition we
    # need an *absolute* signal: how much of the cluster's total resources does
    # this worker hold?
    #
    #   cap_score(wn) = free_cpu(wn)/cluster_cpu + free_mem(wn)/cluster_mem
    #
    # A worker with 12 CPU out of 12.1 cluster CPU scores ~0.99 while a worker
    # with 0.1 CPU scores ~0.008 — correctly reflecting their actual capacity.
    cluster_cpu = sum(worker_res[wn]["cpu"] for wn in worker_list) or 1e-9
    cluster_mem = sum(worker_res[wn]["mem"] for wn in worker_list) or 1e-9

    def _cap_score(wn: str) -> float:
        return _free_cpu(wn) / cluster_cpu + _free_mem(wn) / cluster_mem

    log.info(
        f"   Partition scoring weights: "
        f"w1(topology)={w1:.3f}  w2(resource)={w2:.3f}  "
        f"(cv_edges={cv_edges_local:.3f}  cv_resources={cv_resources_local:.3f})"
    )

    # ── Count anchor votes per (partition, worker) ────────────────────────────
    partition_anchor_votes: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for name in pre_assigned:
        part = node_to_part[name]
        wn   = pre_assigned_worker[name]
        partition_anchor_votes[part][wn] += 1

    # ── Score every (worker, partition) pair and pick the best worker ─────────
    # Process partitions in descending anchor-count order so that anchor-heavy
    # partitions claim their preferred worker first.  After each claim we
    # reduce that worker's virtual capacity so that the resource 
    # correctly steers subsequent anchor-free partitions toward less-loaded
    # workers.  virtual_cap uses cluster-relative units so large workers

    partition_preferred: Dict[int, str] = {}
    virtual_cap: Dict[str, float] = {wn: _cap_score(wn) for wn in worker_list}
    gids_by_anchors = sorted(range(k), key=lambda g: -sum(partition_anchor_votes[g].values()))

    for gid in gids_by_anchors:
        votes      = partition_anchor_votes[gid]
        total_anch = max(sum(votes.values()), 1)
        max_vcap   = max(virtual_cap.values()) or 1.0

        best_wn    = worker_list[0]
        best_score = -1.0
        for wn in worker_list:
            affinity   = votes.get(wn, 0) / total_anch           # ∈ [0, 1]
            norm_cap   = virtual_cap[wn] / max(max_vcap, 1e-9)   # ∈ [0, 1]
            p_score    = w1 * affinity + w2 * norm_cap
            if p_score > best_score:
                best_score = p_score
                best_wn    = wn

        partition_preferred[gid] = best_wn

        # Decrease virtual_cap for the chosen worker so the next partition
        # sees it as proportionally less available.
        virtual_cap[best_wn] = max(0.0, virtual_cap[best_wn] - max_vcap / k)

        log.info(
            f"   Partition {gid}: preferred → {best_wn}  "
            f"(score={best_score:.3f}  w1={w1:.3f}  w2={w2:.3f}  "
            f"cap={_cap_score(best_wn):.3f}  anchors={dict(votes) or 'none'})"
        )

    # ── Extract schedulable nodes into L1 groups ──────────────────────────────
    # Anchors are excluded; their placement is handled separately below.
    l1_groups: Dict[int, List[str]] = defaultdict(list)
    for name, part in zip(all_metis_names, l1_parts_full):
        if name in needs_sched:
            l1_groups[part].append(name)

    # ── Place zero-resource nodes (topology-aware) ────────────────────────────
    # Priority 1 — topology wins:
    #   Use the partition's anchor-steered preferred worker if score > 0
    #   (score > 0 means at least some free cpu or mem fraction remains).
    # Priority 2 — safety fallback:
    #   If the preferred worker is full, use the globally richest worker.
    for name in zero_res_nodes:
        part    = node_to_part[name]
        topo_wn = partition_preferred.get(part, worker_list[part % k])
        if _score(topo_wn) > 0:
            _assign(name, topo_wn)
            log.info(
                f"    ➞ {name} → {topo_wn}  "
                f"(zero-resource, topology-preferred)"
            )
        else:
            fallback = max(worker_list, key=_score)
            _assign(name, fallback)
            log.info(
                f"    ➞ {name} → {fallback}  "
                f"(zero-resource, richest-worker fallback — {topo_wn} full)"
            )

    # ── LEVEL 2: resource-aware recursive assignment ──────────────────────────
    log.info("🟠 L2-METIS: resource-aware recursive assignment")
    for gid in sorted(l1_groups.keys()):
        preferred = partition_preferred[gid]
        gnodes    = l1_groups[gid]
        log.info(f"  Group {gid:02d}: {len(gnodes)} nodes → preferred={preferred}")
        _schedule_group(gnodes, preferred, depth=0)

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("📋 Final worker assignment summary:")
    for wn in worker_list:
        wr = worker_res[wn]
        log.info(
            f"   {wn}: assigned={len(wr['assigned'])}  "
            f"cpu_used={wr['cpu-used']:.3f}  mem_used={wr['mem-used']:.4f} GiB"
        )

    log.info("✅ Hierarchical METIS Scheduling Completed.")
    return config_data


def schedule_workers(
    config_data:  Dict[str, Any],
    etcd_client:  Any,
    epoch_dir:    str = "",
    file_pattern: str = "",
) -> Dict[str, Any]:
    """
    Parameters
    ----------
    config_data  : sat-config dict
    etcd_client  : live etcd3 client
    epoch_dir    : override epoch directory (optional; falls back to config)
    file_pattern : override glob pattern   (optional; falls back to config)
    """
    workers = _get_prefix_data(etcd_client, "/config/workers/")
    if not workers:
        log.error("❌ No workers found in Etcd under /config/workers/")
        sys.exit(1)

    edge_active_count, node_activity = build_epoch_weights(
        config_data, epoch_dir, file_pattern
    )

    alpha, beta, max_depth = auto_alpha_beta(
        edge_active_count = edge_active_count,
        all_nodes         = config_data.get("nodes", {}),
        common_cfg        = config_data.get("node-config-common", {}),
        node_activity     = node_activity,
    )

    return hierarchical_metis_schedule(
        config_data       = config_data,
        edge_active_count = edge_active_count,
        node_activity     = node_activity,
        workers           = workers,
        alpha             = alpha,
        beta              = beta,
        max_depth         = max_depth,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Test the METIS scheduler.\n"
            "Reads sat-config.json + worker-config.json from disk and writes\n"
            "the resulting assignment to an output JSON file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sat-config", default="sat-config.json",
        help="Path to sat-config.json (merged node config).",
    )
    parser.add_argument(
        "--worker-config", default="worker-config.json",
        help="Path to worker-config.json (defines workers and their resources).",
    )
    parser.add_argument("--epoch-dir",    default="epochs")
    parser.add_argument("--file-pattern", default="")
    parser.add_argument("--output",       default="scheduled_config.json")
    parser.add_argument(
        "--alpha", type=float, default=None,
        help=(
            "Edge topology weight multiplier. "
            "If omitted, derived automatically from CV of edge persistence. "
            "Higher → topology locality matters more in METIS node weights."
        ),
    )
    parser.add_argument(
        "--beta", type=float, default=None,
        help=(
            "Node activity weight multiplier. "
            "If omitted, derived automatically from CV of node activity. "
            "Higher → busy nodes attract their neighbours more strongly."
        ),
    )
    parser.add_argument(
        "--max-depth", type=int, default=None,
        help=(
            "Maximum recursion depth for L2 sub-partitioning. "
            "If omitted, derived automatically from log2(n_nodes/n_workers)."
        ),
    )
    return parser


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    args = _build_arg_parser().parse_args()

    try:
        with open(args.sat_config, "r", encoding="utf-8") as fh:
            sat_config = json.load(fh)
    except Exception as exc:
        log.error(f"❌ Failed to load sat-config: {exc}")
        sys.exit(1)

    try:
        with open(args.worker_config, "r", encoding="utf-8") as fh:
            worker_config = json.load(fh)
    except Exception as exc:
        log.error(f"❌ Failed to load worker-config: {exc}")
        sys.exit(1)

    all_nodes = sat_config.get("nodes", {})
    workers   = worker_config.get("workers", {})

    if not all_nodes:
        log.error("❌ No nodes found in sat-config.")
        sys.exit(1)
    if not workers:
        log.error("❌ No workers found in worker-config.")
        sys.exit(1)

    # Phase 1 — epoch weights
    edge_active_count, node_activity = build_epoch_weights(
        sat_config, args.epoch_dir, args.file_pattern
    )

    # Derive alpha / beta / max_depth from data, then override with any
    # values explicitly supplied on the command line.
    alpha_auto, beta_auto, max_depth_auto = auto_alpha_beta(
        edge_active_count = edge_active_count,
        all_nodes         = all_nodes,
        common_cfg        = sat_config.get("node-config-common", {}),
        node_activity     = node_activity,
    )

    alpha     = args.alpha     if args.alpha     is not None else alpha_auto
    beta      = args.beta      if args.beta      is not None else beta_auto
    max_depth = args.max_depth if args.max_depth is not None else max_depth_auto

    if args.alpha is not None:
        log.info(f"🔧 alpha overridden by CLI: {alpha}  (auto was {alpha_auto})")
    if args.beta is not None:
        log.info(f"🔧 beta overridden by CLI:  {beta}  (auto was {beta_auto})")
    if args.max_depth is not None:
        log.info(f"🔧 max_depth overridden by CLI: {max_depth}  (auto was {max_depth_auto})")

    log.info(f"Parameters  alpha={alpha}  beta={beta}  max_depth={max_depth}")

    # Phase 2 + 3 — schedule
    scheduled_cfg = hierarchical_metis_schedule(
        config_data       = sat_config,
        edge_active_count = edge_active_count,
        node_activity     = node_activity,
        workers           = workers,
        alpha             = alpha,
        beta              = beta,
        max_depth         = max_depth,
    )

    try:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(scheduled_cfg, fh, indent=2)
        log.info(f"✅ Scheduled config written to {args.output}")
    except Exception as exc:
        log.error(f"❌ Failed to write output config: {exc}")
        sys.exit(1)