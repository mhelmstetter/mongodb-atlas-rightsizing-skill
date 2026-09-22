#!/usr/bin/env python3
"""
MongoDB Atlas rightsizing report.

Pulls hardware measurements for one or more clusters from the Atlas Admin API,
compares them against tier-appropriate thresholds (see ../references/thresholds.md),
and writes a human-readable report.md and a machine-readable report.json.

Auth (pick one):
  Service account (preferred):
    --client-id / --client-secret, or env ATLAS_CLIENT_ID / ATLAS_CLIENT_SECRET
  API key (legacy, HTTP Digest):
    --public-key / --private-key, or env ATLAS_PUBLIC_KEY / ATLAS_PRIVATE_KEY

This script is READ-ONLY. It never modifies a cluster.

Usage:
  python rightsizing.py --group-id <id> --cluster mycluster --days 7 --out-dir ./report
  python rightsizing.py --group-id <id> --days 30 --out-dir ./report   # all clusters in project
  python rightsizing.py --group-id <id> --cluster mycluster --hours 2 --out-dir ./report
      # quick/ad-hoc check of a live situation — always reports 'low' confidence, not a
      # substitute for the default 7-day audit

Only dependency: `requests` (pip install requests).
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    import requests
    from requests.auth import HTTPDigestAuth
except ImportError:
    sys.exit("This script requires the 'requests' package: pip install requests")

ATLAS_BASE = "https://cloud.mongodb.com/api/atlas/v2"
API_VERSION_HEADER = {"Accept": "application/vnd.atlas.2025-03-12+json"}
# ^ Pin an API version header per Atlas Admin API versioning practice. Update the date if the
#   fields this script relies on need a newer schema version. Unversioned requests get the
#   latest version, which can change response shape without notice.

# Host-level metrics: /groups/{groupId}/processes/{processId}/measurements
HOST_METRICS = [
    "SYSTEM_NORMALIZED_CPU_USER",
    "SYSTEM_NORMALIZED_CPU_KERNEL",
    "SYSTEM_MEMORY_USED",
    "SYSTEM_MEMORY_FREE",
    "CONNECTIONS",
    "TICKETS_AVAILABLE_READS",   # context only — NOT a trigger, see evaluate_cluster() for why
    "TICKETS_AVAILABLE_WRITE",  # NB: singular "WRITE", unlike the plural "READS" — an Atlas API quirk
    "GLOBAL_LOCK_CURRENT_QUEUE_READERS",  # ops actually waiting for a concurrency slot — the real trigger
    "GLOBAL_LOCK_CURRENT_QUEUE_WRITERS",
    "OP_EXECUTION_TIME_READS",  # context only — corroborates queuing with actual latency impact
    "OP_EXECUTION_TIME_WRITES",
    "EXTRA_INFO_PAGE_FAULTS",   # rate, SCALAR_PER_SECOND: direct evidence WT cache < working set
    "CACHE_BYTES_READ_INTO",    # rate, BYTES_PER_SECOND: corroborates faults are cache-driven, context only
    "CACHE_FILL_RATIO",         # units=PERCENT, already 0-100 (NOT a 0-1 fraction) — WT overall cache fill
    "DIRTY_FILL_RATIO",         # units=PERCENT, already 0-100 — WT dirty (uncheckpointed) bytes in cache
    "SYSTEM_NORMALIZED_CPU_IOWAIT",  # context only — corroborates disk latency with actual CPU stall time
]

# Disk-level metrics: /groups/{groupId}/processes/{processId}/disks/{partitionName}/measurements
# (querying these against the process-level /measurements endpoint 404s with INVALID_METRIC_NAME)
DISK_METRICS = [
    "DISK_PARTITION_IOPS_READ",
    "DISK_PARTITION_IOPS_WRITE",
    # NOTE: this is disk *capacity* used (bytes/(bytes+free)*100 — verified to match exactly), NOT
    # I/O busy-time/saturation despite what the name suggests. An earlier version of this script
    # substituted this in as a stand-in for the literal (404, doesn't exist) "DISK_PARTITION_
    # UTILIZATION" name and treated it as an I/O-saturation signal in the report text — it isn't
    # one. Kept as a legitimate disk-space trigger (see evaluate_cluster()), just correctly named.
    "DISK_PARTITION_SPACE_PERCENT_USED",
    "DISK_PARTITION_SPACE_USED",
    "DISK_PARTITION_SPACE_FREE",
    "DISK_PARTITION_LATENCY_READ",     # ms per op — the real "is the disk keeping up" signal
    "DISK_PARTITION_LATENCY_WRITE",
    "DISK_PARTITION_THROUGHPUT_READ",  # bytes/sec — context only, no provisioned-throughput
    "DISK_PARTITION_THROUGHPUT_WRITE", # ceiling exposed by Atlas to compare against (unlike IOPS)
]

METRICS = HOST_METRICS + DISK_METRICS

# The HOST_METRICS a mongos exposes (verified against a live mongos's measurement list): it has no
# storage engine, so no WiredTiger cache/ticket/lock-queue/op-execution series — requesting those
# 404s with INVALID_METRIC_NAME.
ROUTER_METRICS = [
    "SYSTEM_NORMALIZED_CPU_USER",
    "SYSTEM_NORMALIZED_CPU_KERNEL",
    "SYSTEM_MEMORY_USED",
    "SYSTEM_MEMORY_FREE",
    "CONNECTIONS",
    "EXTRA_INFO_PAGE_FAULTS",
    "SYSTEM_NORMALIZED_CPU_IOWAIT",
]

# A verdict needs these to have at least one data point. If any is empty, the cluster/shard gets
# insufficient_data — otherwise every check guarded on that metric silently doesn't run and the
# result falls through to "no thresholds crossed".
CORE_METRICS = [
    "SYSTEM_NORMALIZED_CPU_TOTAL",   # derived: user + kernel per data point
    "SYSTEM_MEMORY_FREE_PERCENT",    # derived: free / (free + used) per data point
    "CONNECTIONS",
    "DISK_PARTITION_IOPS_TOTAL",     # derived: read + write per data point
    "DISK_PARTITION_SPACE_PERCENT_USED",
]

# Fraction of returned data points that must be non-null for every core metric; below this the
# window has gaps (restarts, pauses, cluster newer than the window) and confidence is 'low'.
MIN_COVERAGE = 0.90

# An observed value within this fraction of a threshold (either side) is "near the boundary" —
# references/thresholds.md says that caps confidence at 'low'.
NEAR_THRESHOLD_BAND = 0.10

# Scale-up kinds that can be addressed by changing storage/IOPS without a compute tier change.
DISK_ONLY_KINDS = {"iops", "disk_space", "disk_latency"}

MAX_RETRIES = 6  # for 429 rate-limit responses in api_get()

# Max concurrent connections per tier, for the CONNECTIONS trigger below.
# VERIFICATION STATUS: only M10 has been independently confirmed in this project, by reading
# db_diagnostics.py's live serverStatus().connections against a real M10 cluster (current=64 +
# available=1436 = 1500 exactly). The rest are MongoDB's commonly published Atlas tier connection
# limits, NOT independently re-verified here — Atlas has changed these before, and this session
# found several other "commonly known" API details that turned out wrong on inspection (metric
# names, units, ticket semantics). Confirm against a live cluster or
# https://www.mongodb.com/docs/atlas/reference/free-shared-limitations/ (and the equivalent
# dedicated-tier limits page) before treating anything but M10 as authoritative.
TIER_MAX_CONNECTIONS = {
    "M10": 1500,   # empirically verified
    "M20": 3000, "M30": 3000, "M40": 6000, "M50": 16000, "M60": 32000, "M80": 64000,
}


def get_session(args):
    session = requests.Session()
    session.headers.update(API_VERSION_HEADER)

    client_id = args.client_id or os.environ.get("ATLAS_CLIENT_ID")
    client_secret = args.client_secret or os.environ.get("ATLAS_CLIENT_SECRET")
    public_key = args.public_key or os.environ.get("ATLAS_PUBLIC_KEY")
    private_key = args.private_key or os.environ.get("ATLAS_PRIVATE_KEY")

    if client_id and client_secret:
        token = _get_oauth_token(client_id, client_secret)
        session.headers.update({"Authorization": f"Bearer {token}"})
    elif public_key and private_key:
        session.auth = HTTPDigestAuth(public_key, private_key)
    else:
        sys.exit(
            "No credentials found. Provide --client-id/--client-secret or "
            "--public-key/--private-key (or the matching ATLAS_* env vars)."
        )
    return session


def _get_oauth_token(client_id, client_secret):
    resp = requests.post(
        "https://cloud.mongodb.com/api/oauth/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(session, path, params=None):
    """GET an Atlas Admin API path (relative to ATLAS_BASE). Retries 429 rate-limit responses
    (auditing many processes/partitions can hit Atlas's per-project limit); any other error, or a
    429 that persists past MAX_RETRIES, exits — a partial metric set must never be evaluated as if
    it were complete."""
    for attempt in range(MAX_RETRIES + 1):
        resp = session.get(f"{ATLAS_BASE}{path}", params=params, timeout=30)
        if resp.status_code == 429 and attempt < MAX_RETRIES:
            # Atlas's per-project token bucket (300 requests, refilling at 100/minute) can answer
            # "Retry-After: 0" while still empty, so back off at least 1, 2, 4, ... 32s (63s total).
            time.sleep(max(int(resp.headers.get("Retry-After", 0)), 2 ** attempt))
            continue
        if not resp.ok:
            sys.exit(f"Atlas API error {resp.status_code} on {path}: {resp.text[:500]}")
        return resp.json()


def api_get_all(session, path):
    """GET every page of a paginated Atlas list endpoint."""
    results, page = [], 1
    while True:
        data = api_get(session, path, params={"itemsPerPage": 500, "pageNum": page})
        results.extend(data["results"])
        if len(results) >= data["totalCount"] or not data["results"]:
            break
        page += 1
    if len(results) != data["totalCount"]:
        sys.exit(f"Atlas reported totalCount={data['totalCount']} on {path} but returned "
                 f"{len(results)} results across {page} pages.")
    return results


def list_clusters(session, group_id):
    return api_get_all(session, f"/groups/{group_id}/clusters")


def get_cluster(session, group_id, cluster_name):
    return api_get(session, f"/groups/{group_id}/clusters/{cluster_name}")


# Atlas host labels: <prefix>-shard-<NN>-<NN> for replica-set and shard members (and mongos, on a
# different port), <prefix>-config-<NN>-<NN> for dedicated config servers.
HOST_LABEL_RE = r"^{prefix}-(shard|config)-\d+-\d+$"


def cluster_host_pattern(cluster_cfg):
    """Derive (host-label regex, domain) from the cluster's own standard connection string.

    Atlas hostnames don't always start with the cluster name verbatim (it's lowercased and
    truncated, and gets a suffix on collision), so matching processes by name substring picks up
    other clusters that share a prefix (`prod` vs `prod-analytics`). The connection string carries
    the exact host prefix and project domain this cluster's processes use."""
    standard = cluster_cfg["connectionStrings"]["standard"]
    first_host = standard.split("://", 1)[1].split("/", 1)[0].split(",")[0].split(":")[0]
    label, domain = first_host.split(".", 1)
    # Sharded clusters with an embedded config server list the config hosts (mongos on 27016)
    # first, so the first host can be either form.
    m = re.match(r"^(.+)-(shard|config)-\d+-\d+$", label)
    if not m:
        sys.exit(f"Unexpected host label '{label}' in connection string for cluster "
                 f"{cluster_cfg['name']}; expected '<prefix>-shard-NN-NN' or '<prefix>-config-NN-NN'.")
    prefix = m.group(1)
    return re.compile(HOST_LABEL_RE.format(prefix=re.escape(prefix))), domain


def list_processes_for_cluster(session, group_id, cluster_cfg):
    pattern, domain = cluster_host_pattern(cluster_cfg)
    procs = []
    for p in api_get_all(session, f"/groups/{group_id}/processes"):
        label, _, proc_domain = p["userAlias"].partition(".")
        if proc_domain == domain and pattern.match(label):
            procs.append(p)
    if not procs:
        sys.exit(f"No processes matched cluster {cluster_cfg['name']} (hosts {pattern.pattern} "
                 f"in {domain}).")
    return procs


def node_label(p):
    return f"{p['userAlias'].split('.', 1)[0]}:{p['port']}"


def group_processes_by_replica_set(procs):
    """Split a cluster's processes into per-shard (or single-replica-set) groups, plus routers.

    Every mongod — whether it's a shard member, a config-server member, or a plain replica-set
    member — reports a real `replicaSetName`, and each shard IS its own distinct replica set, so
    grouping by that field reliably separates shard-0's nodes from shard-1's nodes from the config
    server's nodes, with no assumption about hostname numbering. mongos routers are stateless and
    don't belong to a replica set, so they have no `replicaSetName`; they're returned separately
    since they have no local storage/WT cache to evaluate against these thresholds.

    NOTE: this is implemented against the documented Atlas API process schema, not verified against
    a live sharded cluster (none were available to test against at the time this was written). If
    you run this against a real sharded cluster, sanity-check the shard grouping in the output
    against what the Atlas UI shows before trusting the per-shard verdicts.
    """
    shard_groups = {}
    routers = []
    for p in procs:
        rs_name = p.get("replicaSetName")
        if rs_name:
            shard_groups.setdefault(rs_name, []).append(p)
        else:
            routers.append(p)
    return shard_groups, routers


def _fetch_measurements(session, path, metric_names, period, granularity):
    """Returns {metric_name: [value or None, ...]} — one entry per data point Atlas returned,
    INCLUDING null values. Nulls mark gaps (restarts, pauses, or time before the cluster existed)
    and are kept so evaluate_cluster() can measure data coverage, not just the non-null samples."""
    query = [("granularity", granularity), ("period", period)] + [("m", m) for m in metric_names]
    data = api_get(session, path, params=query)
    out = {m["name"]: [dp["value"] for dp in m["dataPoints"]] for m in data["measurements"]}
    missing = [m for m in metric_names if m not in out]
    if missing:
        sys.exit(f"Atlas returned no series for {missing} on {path} — the measurement names may "
                 f"have changed; check the current MeasurementView enum.")
    return out


def _combine(series_a, series_b, fn, label):
    """Point-by-point combination of two series from the same response (same timestamps). A point
    is None if either input is None."""
    if len(series_a) != len(series_b):
        sys.exit(f"Cannot combine {label}: series lengths differ ({len(series_a)} vs {len(series_b)}).")
    return [None if a is None or b is None else fn(a, b) for a, b in zip(series_a, series_b)]


def get_measurements(session, group_id, process_id, period, granularity):
    out = _fetch_measurements(
        session, f"/groups/{group_id}/processes/{process_id}/measurements",
        HOST_METRICS, period, granularity,
    )
    # Derived series, computed per data point before any percentile is taken (a sum or ratio of
    # percentiles isn't the percentile of the sum or ratio).
    out["SYSTEM_NORMALIZED_CPU_TOTAL"] = _combine(
        out["SYSTEM_NORMALIZED_CPU_USER"], out["SYSTEM_NORMALIZED_CPU_KERNEL"],
        lambda u, k: u + k, "CPU user+kernel")
    out["SYSTEM_MEMORY_FREE_PERCENT"] = _combine(
        out["SYSTEM_MEMORY_FREE"], out["SYSTEM_MEMORY_USED"],
        lambda f, u: 100 * f / (f + u), "memory free/(free+used)")

    # Disk metrics live under a per-partition sub-resource, not the process-level endpoint.
    disks = api_get(session, f"/groups/{group_id}/processes/{process_id}/disks")
    for partition in disks["results"]:
        name = partition["partitionName"]
        disk_out = _fetch_measurements(
            session, f"/groups/{group_id}/processes/{process_id}/disks/{name}/measurements",
            DISK_METRICS, period, granularity,
        )
        # Provisioned IOPS is one budget shared by reads and writes.
        disk_out["DISK_PARTITION_IOPS_TOTAL"] = _combine(
            disk_out["DISK_PARTITION_IOPS_READ"], disk_out["DISK_PARTITION_IOPS_WRITE"],
            lambda r, w: r + w, f"IOPS read+write on {name}")
        for k, v in disk_out.items():
            out.setdefault(k, []).extend(v)

    return out


def pctl(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize(points):
    """points is every data point Atlas returned, including nulls (gaps). Percentiles are over the
    non-null values; coverage is the non-null fraction."""
    values = [v for v in points if v is not None]
    if not values:
        return None
    return {
        "p5": round(pctl(values, 0.05), 2),
        "p50": round(pctl(values, 0.5), 2),
        "p95": round(pctl(values, 0.95), 2),
        "max": round(max(values), 2),
        "n": len(values),
        "coverage": round(len(values) / len(points), 3),
    }


def evaluate_cluster(cluster_cfg, process_metrics, window_days):
    """Apply the threshold rules from references/thresholds.md.
    Returns (verdict, reasons, confidence, confidence_notes, summary).

    window_days is the lookback window expressed in days as a float (e.g. a 2-hour window is
    2/24 = 0.083) so the confidence-level thresholds below apply correctly to sub-day windows too.
    """
    agg = {}
    for metrics in process_metrics.values():
        for name, values in metrics.items():
            agg.setdefault(name, []).extend(values)

    summary = {name: summarize(vals) for name, vals in agg.items()}

    # Every threshold check below is guarded on its metric being present, so a missing core metric
    # would silently skip its checks and fall through to "no thresholds crossed". No data for a
    # core metric means no basis for a verdict (e.g. a cluster created minutes before the run).
    missing_core = [m for m in CORE_METRICS if not summary.get(m)]
    if missing_core:
        return (
            "insufficient_data",
            [f"No data in the requested window for: {', '.join(missing_core)}. The cluster/shard "
             f"may be too new (check its creation time), paused, or unreachable. Not a basis for "
             f"any scale-up/down/no-change verdict; re-run once it has some operating history."],
            "low",
            [],
            summary,
        )

    # Every threshold comparison is recorded so confidence can account for values sitting near a
    # boundary, whether or not the check fired.
    checks = []

    def exceeds(label, observed, cutoff, above=True):
        checks.append((label, observed, cutoff))
        return observed > cutoff if above else observed < cutoff

    # (kind, reason) — kind decides between scale_up and change_disk_or_iops below.
    up = []
    # Checks that couldn't run for lack of a ceiling to compare against. Surfaced, and they cap
    # confidence at medium.
    skipped = []

    cpu = summary["SYSTEM_NORMALIZED_CPU_TOTAL"]
    if exceeds("CPU (user+kernel) p95", cpu["p95"], 80):
        up.append(("cpu", f"CPU p95 {cpu['p95']}% (user+kernel) > 80% threshold"))

    # Free memory is low-is-bad, so the tail that matters is the bottom one: p5, not p95.
    mem_free_pct = summary["SYSTEM_MEMORY_FREE_PERCENT"]
    if exceeds("Free memory p5", mem_free_pct["p5"], 10, above=False):
        up.append(("memory", f"Free memory p5 {mem_free_pct['p5']}% < 10% of total"))

    # thresholds.md has documented this trigger since early in this script's history, but it was
    # never actually implemented — CONNECTIONS was pulled into the report table with no threshold
    # logic behind it. See TIER_MAX_CONNECTIONS for verification status of the per-tier ceilings.
    conns = summary["CONNECTIONS"]
    max_conns = cluster_cfg["_max_connections"]
    if max_conns is None:
        skipped.append("Connections check skipped: no max-connections value for this tier in "
                       "TIER_MAX_CONNECTIONS (or shards have mixed tiers).")
    elif exceeds("Connections p95", conns["p95"], 0.8 * max_conns):
        up.append(("connections",
                   f"Connections p95 {conns['p95']} > 80% of tier's {max_conns} max connections"))

    iops = summary["DISK_PARTITION_IOPS_TOTAL"]
    provisioned = cluster_cfg["_provisioned_iops"]
    if provisioned is None:
        skipped.append("IOPS headroom check skipped: the cluster config has no single provisioned "
                       "diskIOPS value to compare against.")
    elif exceeds("IOPS (read+write) p95", iops["p95"], 0.9 * provisioned):
        up.append(("iops", f"Disk IOPS p95 {iops['p95']} (read+write) > 90% of provisioned {provisioned}"))

    # NB: this is disk SPACE capacity used, not I/O busy-time/saturation — see DISK_METRICS
    # comment. Still a legitimate trigger (running out of disk space is a real problem), just
    # named for what it actually measures rather than implying an I/O-saturation signal.
    space_used = summary["DISK_PARTITION_SPACE_PERCENT_USED"]
    if exceeds("Disk space used p95", space_used["p95"], 90):
        up.append(("disk_space", f"Disk space used p95 {space_used['p95']}% > 90% of capacity"))

    # Real I/O-saturation signal: per-operation latency. Unlike IOPS, there's no Atlas-exposed
    # "provisioned throughput" ceiling to compare DISK_PARTITION_THROUGHPUT_READ/WRITE against, so
    # those are pulled for context only; latency is what actually degrades query performance and
    # needs no ceiling to be meaningful. The 15ms cutoff is a heuristic (not derived from MongoDB/
    # Atlas documentation), same treatment as the page-fault threshold — tune to your own baseline.
    for label, latency_name, throughput_name in (
        ("read", "DISK_PARTITION_LATENCY_READ", "DISK_PARTITION_THROUGHPUT_READ"),
        ("write", "DISK_PARTITION_LATENCY_WRITE", "DISK_PARTITION_THROUGHPUT_WRITE"),
    ):
        latency = summary.get(latency_name)
        if latency and exceeds(f"Disk {label} latency p95", latency["p95"], 15):
            throughput = summary.get(throughput_name)
            iowait = summary.get("SYSTEM_NORMALIZED_CPU_IOWAIT")
            corroboration = []
            if throughput and throughput["p95"] > 0:
                corroboration.append(f"{throughput_name} p95 {throughput['p95']} B/s")
            if iowait and iowait["p95"] > 5:
                corroboration.append(f"CPU iowait p95 {iowait['p95']}%")
            note = f" ({', '.join(corroboration)} corroborate)" if corroboration else ""
            up.append(("disk_latency",
                       f"Disk {label} latency p95 {latency['p95']}ms > 15ms heuristic{note}"))

    # TICKETS_AVAILABLE_READS/WRITE are pulled for context (below) but are NOT a trigger. On
    # MongoDB 7.0+, WiredTiger's execution control dynamically resizes the concurrency ticket
    # pool based on observed throughput, replacing the old static 128/128 pool. A low "tickets
    # available" reading no longer reliably means exhaustion — it can just as easily mean the
    # algorithm sized the pool small because the workload is genuinely light. Confirmed on this
    # skill's own test cluster: TICKETS_AVAILABLE_READS held at p50=4 for a week while
    # GLOBAL_LOCK_CURRENT_QUEUE_READERS stayed at p95=0/max=0 the entire time — i.e. nothing was
    # ever actually waiting. Queue depth is what changed: it measures the real symptom (ops
    # waiting for a concurrency slot) rather than an internal, version-dependent pool-sizing
    # artifact, and it means the same thing on MongoDB 6.x's static pool as on 7.0+'s dynamic one.
    queue_r = summary.get("GLOBAL_LOCK_CURRENT_QUEUE_READERS")
    queue_w = summary.get("GLOBAL_LOCK_CURRENT_QUEUE_WRITERS")
    for label, q in (("read", queue_r), ("write", queue_w)):
        if q and q["p95"] > 0:
            exec_time = summary.get(f"OP_EXECUTION_TIME_{label.upper()}S")
            corroboration = (
                f", OP_EXECUTION_TIME_{label.upper()}S p95 {exec_time['p95']}ms corroborates latency impact"
                if exec_time and exec_time["p95"] > 0 else ""
            )
            up.append(("queue",
                       f"{label.capitalize()} concurrency queue p95 {q['p95']} > 0 — operations are "
                       f"actually waiting for a WiredTiger ticket, not just a low available count{corroboration}"))

    # Not in the original skill's thresholds.md — added as a heuristic (see references/thresholds.md
    # "WiredTiger cache pressure" section). Page faults/sec is cheap on Atlas's SSD-backed storage, so
    # this is intentionally sensitive; treat a lone trigger here as weaker evidence than CPU/IOPS/disk,
    # and note whether CACHE_BYTES_READ_INTO also elevated (context, not itself a trigger).
    page_faults = summary.get("EXTRA_INFO_PAGE_FAULTS")
    if page_faults and exceeds("Page faults p95", page_faults["p95"], 1.0):
        cache_read = summary.get("CACHE_BYTES_READ_INTO")
        corroboration = (
            f", CACHE_BYTES_READ_INTO p95 {cache_read['p95']} B/s corroborates cache misses"
            if cache_read and cache_read["p95"] > 0 else ""
        )
        up.append(("page_faults",
                   f"Page faults p95 {page_faults['p95']}/sec > 1.0/sec heuristic — WiredTiger cache may be "
                   f"undersized for the working set{corroboration}"))

    # WiredTiger eviction thresholds (see references/thresholds.md "WiredTiger eviction thresholds").
    # eviction_target/eviction_dirty_target (80%/5%) are the point where WT's *background* eviction
    # threads start working to bring usage back down — not yet the harder eviction_trigger/
    # eviction_dirty_trigger (95%/20%) point where application threads get recruited to evict. Using
    # the target (not trigger) values here means this fires earlier/more sensitively, on the
    # assumption that sustained background-eviction pressure is itself worth flagging before it
    # escalates to the fully pressured state.
    cache_fill = summary.get("CACHE_FILL_RATIO")
    if cache_fill and exceeds("Cache fill ratio p95", cache_fill["p95"], 80):
        up.append(("cache_fill",
                   f"Cache fill ratio p95 {cache_fill['p95']}% > 80% (eviction_target) — "
                   f"background eviction likely running continuously"))

    dirty_fill = summary.get("DIRTY_FILL_RATIO")
    if dirty_fill and exceeds("Dirty fill ratio p95", dirty_fill["p95"], 5):
        up.append(("dirty_fill",
                   f"Dirty fill ratio p95 {dirty_fill['p95']}% > 5% (eviction_dirty_target) — "
                   f"dirty-page eviction likely running continuously"))

    latency_r = summary.get("DISK_PARTITION_LATENCY_READ")
    latency_w = summary.get("DISK_PARTITION_LATENCY_WRITE")

    # Gaps are computed before the scale-down decision: thresholds.md requires a gap-free window
    # for a scale-down, not just a lower confidence on one.
    low_coverage = {m: summary[m]["coverage"] for m in CORE_METRICS if summary[m]["coverage"] < MIN_COVERAGE}

    down_ok = (
        cpu["p95"] < 20
        and mem_free_pct["p5"] > 40
        and space_used["p95"] < 50
        and provisioned is not None and iops["p95"] < 0.5 * provisioned
        and max_conns is not None and conns["p95"] < 0.3 * max_conns
        and (not page_faults or page_faults["p95"] < 0.5)
        and (not cache_fill or cache_fill["p95"] < 50)
        and (not dirty_fill or dirty_fill["p95"] < 2)
        and (not queue_r or queue_r["max"] == 0)
        and (not queue_w or queue_w["max"] == 0)
        and (not latency_r or latency_r["p95"] < 5)
        and (not latency_w or latency_w["p95"] < 5)
        and not low_coverage
        and window_days >= 7
    )

    up_reasons = [reason for _, reason in up]
    if up and all(kind in DISK_ONLY_KINDS for kind, _ in up):
        # thresholds.md: a disk bottleneck doesn't always need a compute tier change — more IOPS
        # or storage alone may resolve it.
        verdict = "change_disk_or_iops"
        reasons = up_reasons
    elif up:
        verdict = "scale_up"
        reasons = up_reasons
    elif down_ok:
        verdict = "scale_down_candidate"
        reasons = ["CPU, memory, disk, IOPS, and connections all comfortably under scale-down thresholds"]
    else:
        verdict = "no_change"
        reasons = ["No thresholds crossed; metrics are in the comfortable mid-range"]

    # Confidence rules from references/thresholds.md. Low: < 3 days, gaps in the data, or any
    # metric near a threshold boundary. Medium at most if a check was skipped. High: >= 7 days of
    # clean data and either no scale-up or a multi-signal one. Explanations go in confidence_notes,
    # not reasons — merge_verdict.py matches on reason text to tell which triggers fired.
    confidence_notes = list(skipped)
    if low_coverage:
        confidence_notes.append(
            "Gaps in the data (non-null share of data points below "
            f"{MIN_COVERAGE:.0%}): " + ", ".join(f"{m} {c:.0%}" for m, c in low_coverage.items())
            + " — restarts, a pause, or a cluster newer than the window."
        )
    near = [(label, observed, cutoff) for label, observed, cutoff in checks
            if abs(observed - cutoff) <= NEAR_THRESHOLD_BAND * cutoff]
    if near:
        confidence_notes.append(
            f"Near a threshold (within {NEAR_THRESHOLD_BAND:.0%}): "
            + ", ".join(f"{label} {observed:.4g} vs {cutoff:.4g}" for label, observed, cutoff in near)
        )

    if window_days < 3 or low_coverage or near:
        confidence = "low"
    elif window_days >= 7 and not skipped and (not up or len(up) >= 2):
        confidence = "high"
    else:
        confidence = "medium"

    return verdict, reasons, confidence, confidence_notes, summary


CONFIDENCE_ORDER = ["low", "medium", "high"]


def _by_node(per_node, all_nodes):
    """[(node, text), ...] -> one line per distinct text, prefixed with the nodes it applies to
    ("all nodes" when every node has it)."""
    nodes_for = {}
    for node, text in per_node:
        nodes_for.setdefault(text, []).append(node)
    return [f"{'all nodes' if len(nodes) == len(all_nodes) else ', '.join(nodes)}: {text}"
            for text, nodes in nodes_for.items()]


def evaluate_process_group(session, group_id, procs, cfg, period, granularity, window_days):
    """Evaluate one group of processes (a shard, a config-server RS, or a whole non-sharded
    cluster). Each node is evaluated on its own and the results combined: pooling samples across
    nodes before taking p95 lets idle secondaries dilute a hot primary (with two idle secondaries
    the primary is only a third of the samples). Connection limits and provisioned IOPS are
    per-node ceilings too.

    Group verdict: insufficient_data if any node has it, then scale_up, then change_disk_or_iops if
    any node needs it; scale_down_candidate only if every node qualifies, otherwise no_change.
    Confidence is the lowest across nodes."""
    nodes = {}
    for p in procs:
        metrics = get_measurements(session, group_id, p["id"], period, granularity)
        nodes[node_label(p)] = evaluate_cluster(cfg, {p["id"]: metrics}, window_days)

    verdicts = {v for v, _, _, _, _ in nodes.values()}
    if "insufficient_data" in verdicts:
        verdict = "insufficient_data"
    elif "scale_up" in verdicts:
        verdict = "scale_up"
    elif "change_disk_or_iops" in verdicts:
        verdict = "change_disk_or_iops"
    elif verdicts == {"scale_down_candidate"}:
        verdict = "scale_down_candidate"
    else:
        verdict = "no_change"

    if verdict == "insufficient_data":
        reasons = _by_node([(node, reason) for node, (v, node_reasons, _, _, _) in nodes.items()
                            if v == verdict for reason in node_reasons], nodes)
    elif verdict in ("scale_up", "change_disk_or_iops"):
        reasons = _by_node([(node, reason) for node, (v, node_reasons, _, _, _) in nodes.items()
                            if v in ("scale_up", "change_disk_or_iops") for reason in node_reasons], nodes)
    elif verdict == "scale_down_candidate":
        reasons = ["CPU, memory, disk, IOPS, and connections all comfortably under scale-down "
                   "thresholds on every node"]
    else:
        reasons = ["No thresholds crossed on any node; metrics are in the comfortable mid-range"]

    confidence = min((c for _, _, c, _, _ in nodes.values()), key=CONFIDENCE_ORDER.index)
    confidence_notes = _by_node([(node, note) for node, (_, _, _, notes, _) in nodes.items()
                                 for note in notes], nodes)
    summary = {node: node_summary for node, (_, _, _, _, node_summary) in nodes.items()}
    return verdict, reasons, confidence, confidence_notes, summary


def evaluate_router_group(session, group_id, procs, period, granularity):
    """mongos routers have no local storage or WiredTiger cache, so the scale-up/down thresholds
    in evaluate_cluster() (disk, tickets, cache fill, etc.) don't apply to them — evaluating a
    router's CPU/connections against the same rules would conflate router capacity with shard
    capacity. Report their host-level metrics per router for visibility, but skip a scored verdict.

    NOT verified against a live sharded cluster — see group_processes_by_replica_set()'s docstring.
    """
    summary = {}
    for p in procs:
        m = _fetch_measurements(
            session, f"/groups/{group_id}/processes/{p['id']}/measurements",
            ROUTER_METRICS, period, granularity,
        )
        summary[node_label(p)] = {name: summarize(vals) for name, vals in m.items()}
    return summary


def build_report(group_id, results, window_label):
    lines = [f"# Atlas Rightsizing Report", "",
             f"Project: `{group_id}`  |  Lookback: {window_label}  |  "
             f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}", ""]
    for r in results:
        lines.append(f"## {r['cluster']}  —  {r['current_tier']}")
        lines.append(f"**Verdict: {r['verdict'].replace('_', ' ').upper()}**  (confidence: {r['confidence']})")
        lines.append("")
        for reason in r["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")
        if r["confidence_notes"]:
            lines.append("Confidence notes:")
            for note in r["confidence_notes"]:
                lines.append(f"- {note}")
            lines.append("")
        nodes = list(r["metric_summary"])
        if nodes:
            lines.append("Per node — p50 / p95 / max (coverage):")
            lines.append("")
            lines.append("| Metric | " + " | ".join(nodes) + " |")
            lines.append("|---|" + "---|" * len(nodes))
            metric_names = list(dict.fromkeys(
                name for node in nodes for name in r["metric_summary"][node]))
            for name in metric_names:
                cells = []
                for node in nodes:
                    s = r["metric_summary"][node].get(name)
                    cells.append(f"{s['p50']} / {s['p95']} / {s['max']} ({s['coverage']:.0%})"
                                 if s else "no data")
                lines.append(f"| {name} | " + " | ".join(cells) + " |")
            lines.append("")
    lines.append("---")
    lines.append("This is a read-only recommendation. No cluster was modified. Verify thresholds")
    lines.append("against your own risk tolerance before acting — see references/thresholds.md.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group-id", required=True, help="Atlas project (group) ID")
    ap.add_argument("--cluster", help="Cluster name; omit to audit every cluster in the project")
    ap.add_argument("--days", type=int, default=7,
                     help="Lookback window in days (default 7). Ignored if --hours is given.")
    ap.add_argument("--hours", type=int, default=None,
                     help="Lookback window in hours, for quick/ad-hoc checks (e.g. --hours 2). "
                          "Overrides --days. Windows under 3 days always report 'low' confidence "
                          "(see references/thresholds.md) — this is for spot-checking a live "
                          "situation, not a substitute for the default 7-day audit.")
    ap.add_argument("--out-dir", default="./rightsizing-report", help="Output directory")
    ap.add_argument("--client-id"); ap.add_argument("--client-secret")
    ap.add_argument("--public-key"); ap.add_argument("--private-key")
    args = ap.parse_args()
    if args.days < 1 or (args.hours is not None and args.hours < 1):
        ap.error("--days and --hours must be positive whole numbers")

    if args.hours is not None:
        window_days = args.hours / 24
        period = f"PT{args.hours}H"
        # Atlas only retains PT1M (1-minute) resolution for ~38-48h back — beyond that it's
        # already rolled up, so requesting PT1M for a longer window would silently return nulls
        # for the older portion. Use the finest resolution Atlas actually has for the window:
        # PT1M captures true instantaneous peaks (coarser granularities average within each
        # bucket and can hide brief spikes — e.g. a queue depth that briefly hit 6 read back as
        # max 2.4 at PT5M and max 1 at PT1H over the same window). Stay a margin under the
        # observed ~38h cutoff to be safe.
        # Only PT1M's retention window was verified against the live API; no PT5M retention cutoff
        # is assumed, so windows beyond it use PT1H.
        granularity = "PT1M" if args.hours <= 36 else "PT1H"
        window_label = f"{args.hours} hours"
    else:
        window_days = args.days
        period = f"P{args.days}D"
        granularity = "PT1H"
        window_label = f"{args.days} days"

    session = get_session(args)
    os.makedirs(args.out_dir, exist_ok=True)

    cluster_names = [args.cluster] if args.cluster else [
        c["name"] for c in list_clusters(session, args.group_id)
    ]

    results = []
    for name in cluster_names:
        cfg = get_cluster(session, args.group_id, name)

        # replicationSpecs has one entry per shard for a SHARDED cluster, one entry total for a
        # REPLICASET — so this works unmodified for either topology. Atlas supports per-shard
        # ("asymmetric") tiers, so don't assume a single tier: report the full set, and flag it
        # explicitly if shards are mixed.
        region_configs = [rc for spec in cfg["replicationSpecs"] for rc in spec["regionConfigs"]]
        tiers = sorted({rc["electableSpecs"]["instanceSize"] for rc in region_configs})
        tier_display = tiers[0] if len(tiers) == 1 else "mixed: " + ", ".join(tiers)
        # Same "single value or skip" logic as provisioned IOPS below — a mixed-tier sharded
        # cluster has no single ceiling to compare a pooled CONNECTIONS reading against.
        max_connections = TIER_MAX_CONNECTIONS.get(tiers[0]) if len(tiers) == 1 else None

        if len(tiers) == 1 and tiers[0] in ("M0", "M2", "M5"):
            results.append({
                "cluster": name, "current_tier": tier_display, "verdict": "not_supported",
                "reasons": ["Free/shared tier does not expose full hardware measurements"],
                "confidence": "n/a", "confidence_notes": [], "metric_summary": {},
            })
            continue

        # diskIOPS is absent for storage without separately provisioned IOPS; that case is
        # surfaced as a skipped check in evaluate_cluster().
        iops_values = sorted({rc["electableSpecs"].get("diskIOPS") for rc in region_configs} - {None})
        # Can't reliably join a specific shard's replicaSetName back to its specific
        # replicationSpecs entry (see group_processes_by_replica_set docstring), so if shards
        # have DIFFERENT provisioned IOPS, don't guess — skip that one threshold check for every
        # group rather than risk comparing a shard's IOPS usage against the wrong shard's ceiling.
        iops_note = None
        if len(iops_values) == 1:
            provisioned_iops = iops_values[0]
        elif len(iops_values) > 1:
            provisioned_iops = None
            iops_note = (
                f"IOPS-provisioned threshold skipped: shards have differing provisioned IOPS "
                f"({', '.join(str(v) for v in iops_values)}) and this script can't confirm which "
                f"shard each replica set maps to — check disk IOPS headroom per-shard in the Atlas UI."
            )
        else:
            provisioned_iops = None

        if cfg["paused"]:
            results.append({
                "cluster": name, "current_tier": tier_display, "verdict": "paused",
                "reasons": ["Cluster is paused — no processes to measure. Resume it and re-run."],
                "confidence": "n/a", "confidence_notes": [], "metric_summary": {},
            })
            continue

        # With autoscaling on, Atlas already moves the tier (or disk size) within the configured
        # bounds, so a scale verdict is about those bounds rather than the current tier.
        autoscaling_notes = []
        compute_as = [rc["autoScaling"]["compute"] for rc in region_configs if rc["autoScaling"]["compute"]["enabled"]]
        if compute_as:
            bounds = sorted({f"{a['minInstanceSize']}–{a['maxInstanceSize']}" for a in compute_as})
            autoscaling_notes.append(
                f"Compute autoscaling is enabled ({', '.join(bounds)}): Atlas already scales the tier "
                f"within these bounds, so a scale verdict means reviewing the bounds, not the tier.")
        if any(rc["autoScaling"]["diskGB"]["enabled"] for rc in region_configs):
            autoscaling_notes.append("Storage autoscaling is enabled: Atlas grows disk automatically, "
                                     "so a disk-space trigger is less urgent.")

        procs = list_processes_for_cluster(session, args.group_id, cfg)
        shard_groups, routers = group_processes_by_replica_set(procs)

        if len(shard_groups) == 1 and not routers:
            # Plain replica set — a single result.
            group_procs = next(iter(shard_groups.values()))
            cfg["_provisioned_iops"] = provisioned_iops
            cfg["_max_connections"] = max_connections
            verdict, reasons, confidence, confidence_notes, summary = evaluate_process_group(
                session, args.group_id, group_procs, cfg, period, granularity, window_days
            )
            reasons = reasons + ([iops_note] if iops_note else []) + autoscaling_notes
            results.append({
                "cluster": name, "current_tier": tier_display, "verdict": verdict,
                "reasons": reasons, "confidence": confidence,
                "confidence_notes": confidence_notes, "metric_summary": summary,
            })
        else:
            # Sharded topology detected (more than one replica set matched, and/or routers present)
            # — evaluate each shard (and the config-server replica set, which will show up as just
            # another group) independently, per SKILL.md's documented per-shard behavior.
            for rs_name in sorted(shard_groups):
                shard_cfg = dict(cfg)
                shard_cfg["_provisioned_iops"] = provisioned_iops
                shard_cfg["_max_connections"] = max_connections
                verdict, reasons, confidence, confidence_notes, summary = evaluate_process_group(
                    session, args.group_id, shard_groups[rs_name], shard_cfg, period, granularity, window_days
                )
                reasons = reasons + ([iops_note] if iops_note else []) + autoscaling_notes
                results.append({
                    "cluster": f"{name} — shard {rs_name}", "current_tier": tier_display,
                    "verdict": verdict, "reasons": reasons, "confidence": confidence,
                    "confidence_notes": confidence_notes, "metric_summary": summary,
                })
            if routers:
                router_summary = evaluate_router_group(session, args.group_id, routers, period, granularity)
                results.append({
                    "cluster": f"{name} — mongos routers ({len(routers)})", "current_tier": tier_display,
                    "verdict": "not_applicable",
                    "reasons": ["mongos routers have no local storage or WiredTiger cache — shown "
                                "for visibility only, not evaluated against rightsizing thresholds"],
                    "confidence": "n/a", "confidence_notes": [], "metric_summary": router_summary,
                })

    report_md = build_report(args.group_id, results, window_label)
    with open(os.path.join(args.out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report_md)
    with open(os.path.join(args.out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump({
            "groupId": args.group_id, "windowDays": window_days, "windowLabel": window_label,
            "results": results,
        }, f, indent=2)

    print(report_md)
    print(f"\n[written to {args.out_dir}/report.md and report.json]")


if __name__ == "__main__":
    main()
