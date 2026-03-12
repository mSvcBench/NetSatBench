#!/usr/bin/env python3
"""
scheduler_metis.py  —  Epoch-driven hierarchical METIS scheduler for NetSatBench
=================================================================================

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
      • METIS integer node-weight = resource_weight + activity_weight
        so that heavy AND topologically central nodes are kept together.

PHASE 3  —  hierarchical_metis_schedule()
    PRE-ASSIGNED nodes  (node config has 'worker' field pointing to a valid worker)
        → resources deducted immediately; node enters METIS as an anchor so
          its edges bias neighbours toward the correct worker.

    ZERO-RESOURCE nodes  (cpu-request == 0  AND  mem-request == 0)
        → kept as METIS anchors so their topology edges influence neighbours.
          Placed on their partition's preferred worker if headroom allows,
          else on the globally richest worker.

    ALL other nodes  →  two-phase METIS placement:
        L1  incremental-k:
            Try k=1 first (all nodes → one worker).
            If they fit within max_load → done, no split needed.
            If not → try k=2, k=3, … up to k=n_workers.
            The minimum k whose partitions all fit is chosen.
            Pre-assigned anchors are included in the graph so their edges
            bias each partition toward the correct worker.
        L2  deploy:
            Each L1 partition is assigned to its preferred worker.
            If a partition still doesn't fit (e.g. due to resource changes
            from pre-assigned deductions) → best_worker fallback per node.


Parameters
----------
config_data : dict
    Merged sat-config (output of merge_node_common_config).
etcd_client : etcd3 client
    Live connection to Etcd; workers are read from /config/workers/.
alpha : float
    Scales edge_active_count into METIS edge weights (auto-derived from CV).
    Higher → topology locality matters more.
beta : float
    Scales node_activity into METIS node weights (auto-derived from CV).
    Higher → busy nodes attract their neighbours more strongly.
max_load : float  (default 1.0)
    Fraction of each worker's capacity available for scheduling  ∈ (0, 1].
    E.g. 0.8 reserves 20 % headroom on every worker for load balancing.
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
import copy
from typing import Any, Dict, List, Optional, Set, Tuple

import pymetis  # noqa: PLC0415

# Single logger for the entire module — never re-declared below.
log = logging.getLogger("nsb-scheduler")


# ──────────────────────────────────────────────────────────────────────────────
# UNIT HELPERS  (identical to the rest of the codebase)
# ──────────────────────────────────────────────────────────────────────────────

def parse_cpu(value) -> float:
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


def parse_mem(value) -> float:
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


def get_prefix_data(etcd, prefix: str) -> Dict[str, Any]:
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

def list_epoch_files(epoch_dir: str, file_pattern: str) -> List[str]:
    if not epoch_dir or not file_pattern:
        return []
    search_path = os.path.join(epoch_dir, file_pattern)

    def _last_num(p: str) -> int:
        nums = re.findall(r"(\d+)", os.path.basename(p))
        return int(nums[-1]) if nums else -1

    return sorted(glob(search_path), key=_last_num)


def load_json(path: str) -> dict:
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

    files = list_epoch_files(ep_dir, ep_pat)

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
        ep = load_json(path)

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
        f"🪢 Epoch weights built: {len(files)} files, "
        f"{len(edge_cnt)} edges with activity, "
        f"{len(node_act)} nodes with activity"
    )
    return dict(edge_cnt), dict(node_act)


# ──────────────────────────────────────────────────────────────────────────────
# ADAPTIVE ALPHA / BETA  —  derive weights from statistical signal strength
# ──────────────────────────────────────────────────────────────────────────────

def cv(values: List[float]) -> float:
    """
    Coefficient of Variation = std_dev / mean.
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

    Why scale to [1, 100]
    ──────────────────────
        METIS uses integer weights. Without scaling, normalised values in (0, 1]
        all round to 0 or 1 — METIS sees a uniform graph and alpha/beta have no effect.

    max_depth
    ─────────
        Safety guard for L2: if a partition unexpectedly doesn't fit its
        preferred worker (e.g. after pre-assigned resource deductions),
        schedule_group may split further. max_depth caps that recursion.
        Derived as ceil(log₂(n/sqrt(n))) + 1, floored at 2.

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
    def percentile(values: List[float], p: float) -> float:
        """Return the p-th percentile (0–100) of a sorted list."""
        if not values:
            return 1.0
        sv = sorted(values)
        idx = (p / 100.0) * (len(sv) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(sv) - 1)
        return sv[lo] + (sv[hi] - sv[lo]) * (idx - lo)

    def median(values: List[float]) -> float:
        return percentile(values, 50)

    # ── alpha: base and ceiling from edge weight distribution ─────────────────
    ''' base_alpha is normalised by max(edge_weights) so it lives in [0, 1].
    This puts alpha and beta on the same scale, preventing one from
    dominating the METIS node-weight formula purely due to unit differences.
   
      node_weight = round(alpha x res_score) + round(beta x act_score)
   
    Without normalisation median(edge)=2 vs median(activity)=20 would make
    beta ~10x heavier than alpha regardless of actual signal strength.'''
    
    # ── alpha ────────────────────────────────────────────────────────────────────
    ''' base_alpha = median(edge_weights) / max(edge_weights)  ∈ (0, 1]
    alpha_norm = min(base_alpha *(1 + cv_edges), 1.0)   CV boost, capped at 1
    alpha      = max(1, alpha_norm *100)                scale to [1, 100]
    High cv_edges → links have different lifetimes → alpha near 100.
    Low cv_edges  → all links equally persistent  → alpha near 1.'''
    
    edge_weights = [float(v) for v in edge_active_count.values()] or [1.0]
    cv_edges     = cv(edge_weights)
    max_edge     = max(edge_weights)

    base_alpha = median(edge_weights) / max_edge          # ∈ (0, 1]
    alpha_norm = min(base_alpha * (1.0 + cv_edges), 1.0)  # CV boost, capped at 1
    alpha      = round(max(1.0, alpha_norm * 100), 2)     # scale to [1, 100]

    # ── beta ─────────────────────────────────────────────────────────────────
    ''' base_beta = median(node_activity) / max(node_activity)  ∈ (0, 1]
    cv_joint = mean(cv_cpu, cv_mem, cv_activity)
    beta = max(1, min(base_beta *(1 + cv_joint), 1.0) *100)  →  [1, 100]'''
    default_cpu  = parse_cpu(common_cfg.get("cpu-request", "0"))
    default_mem  = parse_mem(common_cfg.get("mem-request", "0"))
    cpus         = [parse_cpu(cfg.get("cpu-request")) or default_cpu for cfg in all_nodes.values()]
    mems         = [parse_mem(cfg.get("mem-request")) or default_mem for cfg in all_nodes.values()]
    activities   = [float(v) for v in node_activity.values()] or [1.0]
    max_activity = max(activities)

    cv_joint  = (cv(cpus) + cv(mems) + cv(activities)) / 3.0
    base_beta = median(activities) / max_activity
    beta_norm = min(base_beta * (1.0 + cv_joint), 1.0)
    beta      = round(max(1.0, beta_norm * 100), 2)

    # ── max_depth: derived from graph size ────────────────────────────────────
    # n_workers from pre-assigned nodes is often 0 or 1 and skews the estimate.
    # Heuristic: sqrt(n_nodes) gives a reasonable partition count for most graphs.
    # ceil(log2(avg_group)) + 1 levels suffice to halve groups down to singles.
    n_nodes   = len(all_nodes)
    n_workers  = max(2, round(math.sqrt(n_nodes)))
    avg_group  = max(2, n_nodes / n_workers)
    max_depth  = max(2, math.ceil(math.log2(avg_group)) + 1)

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
    Dict[str, float],   # node_cpu    (effective — avg substituted for zero-resource)
    Dict[str, float],   # node_mem    (effective — avg substituted for zero-resource)
    Dict[str, int],     # node_weight (METIS integer)
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
                   round(alpha * resourcescore(n))
                 + round(beta  * activityscore(n))
               )

    where
        resourcescore(n) = cpu(n)/max_cpu + mem(n)/max_mem   ∈ [0, 2]
        activityscore(n) = node_activity(n) / max_activity   ∈ [0, 1]

    Uses RAW cpu/mem (zero stays zero) for the weight formula.
    Zero-resource nodes keep cpu=0/mem=0 here — their METIS weight strategy
    is decided independently of capacity accounting.

    Pinned-no-resource nodes (worker declared, cpu=0, mem=0) receive
    max_node_weight so METIS treats them as the heaviest node in the graph,
    maximising the pull of their edges toward their pinned worker.
    """
    default_cpu = parse_cpu(common_cfg.get("cpu-request", "0"))
    default_mem = parse_mem(common_cfg.get("mem-request", "0"))

    # ── Raw resource values from config ──────────────────────────────────────
    node_cpu_raw: Dict[str, float] = {}
    node_mem_raw: Dict[str, float] = {}
    for name, cfg in all_nodes.items():
        node_cpu_raw[name] = parse_cpu(cfg.get("cpu-request")) or default_cpu
        node_mem_raw[name] = parse_mem(cfg.get("mem-request")) or default_mem

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
     and the fits_group / best_worker checks in Phase 3.
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
    total_cpu_supply = sum(parse_cpu(w.get("cpu", 0)) for w in workers.values())
    total_mem_supply = sum(parse_mem(w.get("mem", 0)) for w in workers.values())

    log.info("🔍 Resource Analysis:")
    log.info(f"   Nodes: {len(all_nodes)}  "
             f"(zero-resource: {len(zero_resource_nodes)})")
    log.info(
        f"   CPU  demand={total_cpu_demand:.2f}  supply={total_cpu_supply:.2f}  "
        f"{'✅' if total_cpu_supply >= total_cpu_demand else '⚠️  OVERCOMMIT'}"
    )
    log.info(
        f"   MEM  demand={total_mem_demand:.2f} GiB  supply={total_mem_supply:.2f} GiB  "
        f"{'✅' if total_mem_supply >= total_mem_demand else '⚠️  OVERCOMMIT'}"
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
        node_weight[name] = max(1, round(alpha * res_score) + round(beta * act_score))

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

    return node_cpu, node_mem, node_weight


# ──────────────────────────────────────────────────────────────────────────────
# METIS CSR HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def build_csr(
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


def pymetis_partition(
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

    xadj, adjncy, ew, vw = build_csr(node_indices, edge_weight, node_weight)
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
    max_load:          float = 1.0,
) -> Dict[str, Any]:
    """
    Mutates config_data['nodes'] in place: adds / updates the 'worker' field
    for every node.  Returns config_data for convenience.

    Parameters
    ──────────
    max_load : float  (default 1.0)
        Fraction of each worker's capacity that may be used  ∈ (0, 1].
        fits_group and best_worker enforce this ceiling so that
        pre-assigned + zero-resource + schedulable nodes combined
        never exceed  worker_cpu *max_load  (and same for MEM).

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

    SCHEDULABLE    everything else  →  L1 incremental-k finds the minimum
                   number of partitions that fit within max_load, then L2
                   deploys each partition to its anchor-steered preferred worker.
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
    node_cpu, node_mem, node_weight_map = analyse_requirements(
        all_nodes, common_cfg, workers,
        edge_active_count, node_activity,
        alpha=alpha, beta=beta,
    )

    # ── Combined edge weight (epoch count scaled by alpha) ────────────────────
    # METIS edge weights are integers ≥ 1, so we scale the epoch counts by alpha
    combined_ew: Dict[Tuple[int, int], int] = {
        edge_key: max(1, round(v * alpha))
        for edge_key, v in edge_active_count.items()
    }

    # ── Index helpers ─────────────────────────────────────────────────────────
    # Forward and reverse maps between node names and global indices (for edge keys).
    node_map: Dict[str, int] = {n: i for i, n in enumerate(all_nodes)}
    inv_map:  Dict[int, str] = {i: n for n, i in node_map.items()}
    nw_idx:   Dict[int, int] = {
        node_map[n]: w for n, w in node_weight_map.items() if n in node_map
    }

    # ── Worker state table ────────────────────────────────────────────────────
    worker_list = sorted(workers.keys())
    k = len(worker_list)

    worker_resource: Dict[str, Dict] = {}
    for wn in worker_list:
        w_conf  = workers[wn]
        cpu = parse_cpu(w_conf.get("cpu", 0))
        mem = parse_mem(w_conf.get("mem", 0))
        worker_resource[wn] = {
            "cpu":      cpu,
            "mem":      mem,
            "cpu-used": parse_cpu(w_conf.get("cpu-used", 0)),
            "mem-used": parse_mem(w_conf.get("mem-used", 0)),
            "data":     w_conf,
        }

    # ── Helper closures ───────────────────────────────────────────────────────
    def free_cpu(wn: str) -> float:
        return worker_resource[wn]["cpu"] - worker_resource[wn]["cpu-used"]

    def free_mem(wn: str) -> float:
        return worker_resource[wn]["mem"] - worker_resource[wn]["mem-used"]

    def avail_cpu(wn: str) -> float:
        '''Free CPU headroom respecting max_load ceiling.'''
        return worker_resource[wn]["cpu"] * max_load - worker_resource[wn]["cpu-used"]

    def avail_mem(wn: str) -> float:
        '''Free MEM headroom respecting max_load ceiling.'''
        return worker_resource[wn]["mem"] * max_load - worker_resource[wn]["mem-used"]

    def score(wn: str) -> float:
        wr = worker_resource[wn]
        return (avail_cpu(wn) / max(wr["cpu"] * max_load, 1e-9)
              + avail_mem(wn) / max(wr["mem"] * max_load, 1e-9))

    def assign(name: str, wn: str) -> None:
        all_nodes[name]["worker"] = wn
        w = worker_resource[wn]
        
        w["cpu-used"] += node_cpu[name] 
        w["mem-used"] += node_mem[name]  

    def fits_group(wn: str, group: List[str]) -> bool:
        '''True if worker wn has enough headroom (≤ max_load) for the group.'''
        return (
            avail_cpu(wn) >= sum(node_cpu[n] for n in group)
            and avail_mem(wn) >= sum(node_mem[n] for n in group)
        )

    def best_worker(name: str) -> str:
        '''Pick the worker with most headroom that can fit this node.'''
        cands = [
            wn for wn in worker_list
            if avail_cpu(wn) >= node_cpu[name]
            and avail_mem(wn) >= node_mem[name]
        ]
        if cands:
            return max(cands, key=score)
        log.warning(f"  ⚠️  Resource-overcommitting {name}")
        return max(worker_list, key=score)

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
            worker_resource[aw]["cpu-used"] += node_cpu[name]   # effective value
            worker_resource[aw]["mem-used"] += node_mem[name]   # effective value
        elif aw:
            log.warning(
                f"  ⚠️  Node {name} has worker='{aw}' not in workers list "
                "→ will be rescheduled."
            )
            del ncfg["worker"]

    # ── ZERO-RESOURCE nodes ───────────────────────────────────────────────────
    """ cpu==0 AND mem==0, no pinned worker.
     Kept as anchors in the METIS graph so their topology edges influence
     neighbour placement. After L1 we use the METIS partition result to
     steer them toward their topologically correct worker, falling back to
     the richest worker only when that worker lacks headroom."""
     
    default_cpu = parse_cpu(common_cfg.get("cpu-request", "0"))
    default_mem = parse_mem(common_cfg.get("mem-request", "0"))
    zero_res_nodes: Set[str] = {
        n for n in all_nodes
        if n not in pre_assigned
        and (parse_cpu(all_nodes[n].get("cpu-request")) or default_cpu) == 0.0
        and (parse_mem(all_nodes[n].get("mem-request")) or default_mem) == 0.0
    }

    if zero_res_nodes:
        log.info(
            f"  Zero-resource nodes ({len(zero_res_nodes)}): "
            "kept as anchors in METIS graph; "
            "placement: topology-preferred worker if headroom available, else richest worker."
        )

    # ── Schedulable nodes ─────────────────────────────────────────────────────
    needs_sched: List[str] = [
        n for n in all_nodes if n not in pre_assigned and n not in zero_res_nodes
    ]

    if not needs_sched:
        for name in zero_res_nodes:
            assign(name, max(worker_list, key=score))
        return config_data,worker_resource

    # ── L2: deploy each partition, with fallback split if needed ───────────────
    # L1 already guarantees each partition fits its preferred worker under
    # normal conditions. schedule_group handles the edge case where a
    # partition no longer fits (due to pre-assigned resource deductions that
    # occurred after the L1 fit check) by splitting further up to max_depth.
    #
    #   group → FIT on preferred?  → YES : deploy all to preferred
    #                              → NO  : METIS split (up to max_depth)
    #                                      each sub → best-fit worker → recurse
    def schedule_group(group: List[str], preferred: str, depth: int) -> None:
        if not group:
            return
        pad = "    " * (depth + 1)

        # ── FIT CHECK ────────────────────────────────────────────────────────
        if fits_group(preferred, group):
            for n in group:
                assign(n, preferred)
            log.info(f"{pad}✅ {len(group)} nodes → {preferred}")
            return

        # ── DEPTH GUARD ───────────────────────────────────────────────────────
        if depth >= max_depth:
            log.warning(f"{pad}⚠️  max_depth={max_depth} reached; best-fit fallback")
            for n in group:
                assign(n, best_worker(n))
            return

        # ── METIS SPLIT ───────────────────────────────────────────────────────
        # Split into at most k=len(workers) parts so each sub-group targets
        # one worker. METIS uses topology+resource weights to minimise cut.
        n_sub = min(len(group), k)
        log.info(f"{pad}🔀 depth={depth}: {len(group)} nodes → {n_sub} sub-groups")

        g_idx = [node_map[n] for n in group if n in node_map]
        parts = pymetis_partition(g_idx, combined_ew, nw_idx, n_sub)

        subs: Dict[int, List[str]] = defaultdict(list)
        for li, part in enumerate(parts):                   # local index in group, part is sub-group id 
            subs[part].append(inv_map[g_idx[li]])

        # Sort sub-groups largest-first; assign each to the best-fit worker
        # (most free resources), then recurse.
        for sub_nodes in sorted(subs.values(), key=len, reverse=True):
            wn = next(
                (w for w in sorted(worker_list, key=score, reverse=True)
                 if fits_group(w, sub_nodes)),
                max(worker_list, key=score),   # fallback: richest
            )
            log.info(f"{pad}  → {len(sub_nodes)} nodes → {wn}")
            schedule_group(sub_nodes, wn, depth + 1)

    # ── L1: incremental METIS — find minimum k that fits ─────────────────────
    # Start with k=1 (no split). If all nodes fit on one worker, done.
    # Otherwise increment k until every partition fits its preferred worker,
    # up to k=n_workers. This avoids unnecessary splits.
    #
    #   k=1 → one partition → FIT on best worker → done
    #   k=2 → two partitions → each FIT         → done
    #   ...
    #   k=n_workers → full split

    all_metis_idx:   List[int] = [node_map[n] for n in all_nodes if n in node_map]
    all_metis_names: List[str] = [inv_map[i] for i in all_metis_idx]

    chosen_k         = None
    l1_parts_full    = None

    for trial_k in range(1, k + 1):
        log.info(
            f"🔵 L1-METIS: trying k={trial_k}  "
            f"({len(needs_sched)} schedulable + "
            f"{len(pre_assigned) + len(zero_res_nodes)} anchors)"
        )
        trial_parts = pymetis_partition(all_metis_idx, combined_ew, nw_idx, trial_k)

        # Group schedulable nodes by partition
        trial_groups: Dict[int, List[str]] = defaultdict(list)
        for name, part in zip(all_metis_names, trial_parts):
            if name in needs_sched or name in zero_res_nodes:
                trial_groups[part].append(name)

        # For k=1 check the single best worker; for k>1 use scoring below.
        # Quick fit check: sort workers by score, assign greedily.
        # L1 fit check simulation
        ''' temp_score is a snapshot of score(wn) that reflects the incremental assignment 
        we use it becouse score(wn) changes as we tentatively assign groups to workers in 
        this simulation. We want to always pick the worker with the highest current score 
        for the next group, which is what the temp_score function allows us to do by using a 
        snapshot of the worker resources that we update as we tentatively assign groups.'''
        
        def temp_score(wn):
            wr = temp_worker_resource[wn]
            avail_c = wr["cpu"] * max_load - wr["cpu-used"]
            avail_m = wr["mem"] * max_load - wr["mem-used"]
            return (avail_c / max(wr["cpu"] * max_load, 1e-9)
                + avail_m / max(wr["mem"] * max_load, 1e-9))
        
        fits = True
        temp_worker_resource = copy.deepcopy(worker_resource)  # snapshot
        for gid, gnodes in sorted(trial_groups.items(), key=lambda x: -len(x[1])):
            placed = False
            for wn in sorted(worker_list, key=temp_score, reverse=True):
                cpu_needed = sum(node_cpu[n] for n in gnodes)
                mem_needed = sum(node_mem[n] for n in gnodes)
                avail_c = temp_worker_resource[wn]["cpu"] * max_load - temp_worker_resource[wn]["cpu-used"]
                avail_m = temp_worker_resource[wn]["mem"] * max_load - temp_worker_resource[wn]["mem-used"]
                if avail_c >= cpu_needed and avail_m >= mem_needed:
                    temp_worker_resource[wn]["cpu-used"] += cpu_needed
                    temp_worker_resource[wn]["mem-used"] += mem_needed
                    
                    placed = True
                    break
            if not placed:
                fits = False
                break   
        if fits:
            chosen_k = trial_k
            l1_parts_full = trial_parts
            log.info(f"✅ L1 fit successful with k={trial_k}")
            break
        else:
            log.info(f"⚠️  L1 fit failed with k={trial_k}")


    if chosen_k is None:
        # Cluster is overcommitted — use full k and let L2 handle overcommit.
        log.warning("⚠️  No k fits without overcommit — using k=n_workers, L2 will overcommit")
        chosen_k      = k
        l1_parts_full = pymetis_partition(all_metis_idx, combined_ew, nw_idx, k)

    # Build name → partition-id lookup used by both anchor-steering and
    # zero-resource topology placement below.
    node_to_part: Dict[str, int] = {
        name: part for name, part in zip(all_metis_names, l1_parts_full)
    }

    # ── Dual-objective preferred worker per L1 partition ─────────────────────
    """ For each partition we pick the worker that maximises:
    
       partitionscore(worker, partition) =
           w1 * anchor_affinity(worker, partition)   ← topology signal
         + w2 * norm_freescore(worker)              ← resource signal
    
     Weights w1 / w2 are derived from the CV of the input data so that
     whichever signal has higher variance (= more discriminating power)
     gets proportionally more influence:
    
       cv_resources = mean(cv_cpu, cv_mem)   from analyse_requirements
       w1 = cv_edges     / (cv_edges + cv_resources)   topology weight
       w2 = cv_resources / (cv_edges + cv_resources)   resource weight
    
     anchor_affinity(worker, partition) =
       votes(worker, partition) / max(total_anchors_in_partition, 1)  ∈ [0,1]
    
     norm_freescore(worker) = score(worker) / 2.0  ∈ [0,1]
       (score returns cpu_frac + mem_frac, each ∈ [0,1], so max = 2)
    
     This means:
      • A partition with many anchors on one worker → topology wins → that worker
      • A partition with no anchors → resource wins → richest worker
      • Mixed cases → continuous blend decided by CV ratio from data

    # ── Compute w1 / w2 from CV of edges and resources ────────────────────────
    # Re-use the same CV values that auto_alpha_beta computed for consistency."""
    
    default_cpu = parse_cpu(common_cfg.get("cpu-request", "0"))
    default_mem = parse_mem(common_cfg.get("mem-request", "0"))
    cpus = [parse_cpu(cfg.get("cpu-request")) or default_cpu
             for cfg in all_nodes.values()]
    mems = [parse_mem(cfg.get("mem-request")) or default_mem
             for cfg in all_nodes.values()]

    cv_edges_local     = cv([float(v) for v in edge_active_count.values()] or [0.0])
    cv_resources_local = (cv(cpus) + cv(mems)) / 2.0
    denom = cv_edges_local + cv_resources_local
    if denom == 0.0:
        w1, w2 = 0.5, 0.5
    else:
        w1 = cv_edges_local     / denom   # topology weight
        w2 = cv_resources_local / denom   # resource weight

    # ── Cluster-relative capacity score for partition selection ─────────────
    ''' score(wn) = free_cpu/own_cpu + free_mem/own_mem  is *relative* to each
     worker's own capacity so that large workers naturally attract more partitions than small ones.
   
      capacity_score (wn) = free_cpu(wn)/cluster_cpu + free_mem(wn)/cluster_mem '''
    
    cluster_cpu = sum(worker_resource[wn]["cpu"] for wn in worker_list) or 1e-9
    cluster_mem = sum(worker_resource[wn]["mem"] for wn in worker_list) or 1e-9

    def capacity_score(wn: str) -> float:
        return free_cpu(wn) / cluster_cpu + free_mem(wn) / cluster_mem

    # ── Count anchor votes per (partition, worker) ────────────────────────────
    partition_anchor_votes: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for name in pre_assigned:
        part = node_to_part[name]
        wn   = pre_assigned_worker[name]
        partition_anchor_votes[part][wn] += 1

    # ── Score every (worker, partition) pair and pick the best worker ─────────
    ''' Process partitions in descending anchor-count order so that anchor-heavy
     partitions claim their preferred worker first.
     After each claim we reduce that worker's virtual capacity so that the resource
     correctly steers subsequent anchor-free partitions toward less-loaded workers. 
     virtual_cap uses cluster-relative units so large workers naturally attract more
     partitions than tiny ones.'''
     
    partition_preferred: Dict[int, str] = {}

    virtual_cap: Dict[str, float] = {wn: capacity_score(wn) for wn in worker_list}

    gids_by_anchors = sorted(range(chosen_k), key=lambda g: -sum(partition_anchor_votes[g].values()))

    for gid in gids_by_anchors:
        votes      = partition_anchor_votes[gid]
        total_anch = max(sum(votes.values()), 1)
        max_vcap   = max(virtual_cap.values()) or 1.0

        best_wn    = worker_list[0]
        bestscore = -1.0
        for wn in worker_list:                                # worker name in worker_list
            affinity   = votes.get(wn, 0) / total_anch           # ∈ [0, 1] 
            norm_cap   = virtual_cap[wn] / max(max_vcap, 1e-9)   # ∈ [0, 1]
            pscore    = w1 * affinity + w2 * norm_cap   # partition score = weighted blend of topology affinity and capacity score
            if pscore > bestscore:
                bestscore = pscore
                best_wn    = wn

        partition_preferred[gid] = best_wn

        # Decrease virtual_cap for the chosen worker so the next partition
        # sees it as proportionally less available. max => for safety if formula < 0:
        virtual_cap[best_wn] =max(0.0, virtual_cap[best_wn] - max_vcap / chosen_k)
        log.info( f"   Partition {gid}: preferred → {best_wn}" )

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
        if score(topo_wn) > 0:
            assign(name, topo_wn)
            log.info(
                f"    ➞ {name} → {topo_wn}  "
                f"(zero-resource, topology-preferred)"
            )
        else:
            fallback = max(worker_list, key=score)
            assign(name, fallback)
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
        schedule_group(gnodes, preferred, depth=0)

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("📋 Final worker assignment summary:")
    for wn in worker_list:
        wr = worker_resource[wn]
        log.info(
            f"   {wn}: cpu_used={wr['cpu-used']:.3f}  mem_used={wr['mem-used']:.4f} GiB"
        )

    log.info("✅ Hierarchical METIS Scheduling Completed.")
    return config_data , worker_resource


def schedule_workers(
    config_data:  Dict[str, Any],
    etcd_client:  Any,
    epoch_dir:    str   = "",
    file_pattern: str   = "",
    alpha:        float = None,
    beta:         float = None,
    max_depth:    int   = None,
    max_load:     float = 1.0,
) -> Dict[str, Any]:
 
    workers = get_prefix_data(etcd_client, "/config/workers/")
    if not workers:
        log.error("❌ No workers found in Etcd under /config/workers/")
        sys.exit(1)

    edge_active_count, node_activity = build_epoch_weights( config_data, epoch_dir, file_pattern)

    alpha_auto, beta_auto, max_depth_auto = auto_alpha_beta(
        edge_active_count = edge_active_count,
        all_nodes         = config_data.get("nodes", {}),
        common_cfg        = config_data.get("node-config-common", {}),
        node_activity     = node_activity,
    )
    if alpha     is None: alpha     = alpha_auto
    if beta      is None: beta      = beta_auto
    if max_depth is None: max_depth = max_depth_auto
    
    scheduled_cfg, worker_resource = hierarchical_metis_schedule(
        config_data       = config_data,
        edge_active_count = edge_active_count,
        node_activity     = node_activity,
        workers           = workers,
        alpha             = alpha,
        beta              = beta,
        max_depth         = max_depth,
        max_load          = max_load,
    )
    # After scheduling, update ETCD with the new worker resource usage.
    # round them to 3 decimal places for CPU and 4 in GiB
    for wn, wr in worker_resource.items():
        worker_cfg = wr["data"]
        worker_cfg["cpu-used"] = round(wr["cpu-used"], 3)
        used_gib = round(wr['mem-used'], 4) # Convert mem-used back to GiB string 
        worker_cfg['mem-used'] = f"{used_gib}GiB"
        
        
        key = f"/config/workers/{wn}"
        etcd_client.put(key, json.dumps(worker_cfg))
        log.info(f"  Saved resources to ETCD for {wn} -> CPU: {worker_cfg['cpu-used']} | MEM: {worker_cfg['mem-used']}")

    return scheduled_cfg


# ──────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--max-load", type=float, default=1.0,
        help=(
            "Maximum fraction of each worker capacity to use  (0.0–1.0, default 1.0). "
            "E.g. 0.8 reserves 20%% headroom on every worker for load balancing."
        ),
    )
    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    args = build_arg_parser().parse_args()

    try:
        with open(args.sat_config, "r", encoding="utf-8") as fh: # open the sat-config.json file for reading (file handle is fh)
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

    alpha_auto, beta_auto, max_depth_auto = auto_alpha_beta(
        edge_active_count = edge_active_count,
        all_nodes         = all_nodes,
        common_cfg        = sat_config.get("node-config-common", {}),
        node_activity     = node_activity,
    )

    alpha = args.alpha     if args.alpha     is not None else alpha_auto
    beta  = args.beta      if args.beta      is not None else beta_auto
    max_depth = args.max_depth if args.max_depth is not None else max_depth_auto
    max_load  = args.max_load
    
    log.info(f" Parameters  alpha={alpha}  beta={beta}  max_depth={max_depth}  max_load={max_load:.0%}")

    # Phase 2 + 3 — schedule
    scheduled_cfg, worker_resource  = hierarchical_metis_schedule(
        config_data       = sat_config,
        edge_active_count = edge_active_count,
        node_activity     = node_activity,
        workers           = workers,
        alpha             = alpha,
        beta              = beta,
        max_depth         = max_depth,
        max_load          = max_load,
    )
        
    try:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(scheduled_cfg, fh, indent=2)
        log.info(f"✅ Scheduled config written to {args.output}")
    except Exception as exc:
        log.error(f"❌ Failed to write output config: {exc}")
        sys.exit(1)