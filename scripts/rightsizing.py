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

Only dependency: `requests` (pip install requests).
"""

import argparse
import json
import os
import statistics
import sys
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

METRICS = [
    "SYSTEM_NORMALIZED_CPU_USER",
    "SYSTEM_NORMALIZED_CPU_KERNEL",
    "SYSTEM_MEMORY_USED",
    "SYSTEM_MEMORY_FREE",
    "DISK_PARTITION_IOPS_READ",
    "DISK_PARTITION_IOPS_WRITE",
    "DISK_PARTITION_UTILIZATION",
    "DISK_PARTITION_SPACE_USED",
    "DISK_PARTITION_SPACE_FREE",
    "CONNECTIONS",
    "TICKETS_AVAILABLE_READS",
    "TICKETS_AVAILABLE_WRITES",
]

TIER_SPECS = {
    "M10": (2, 2), "M20": (2, 4), "M30": (2, 8), "M40": (4, 16),
    "M50": (8, 32), "M60": (16, 64), "M80": (32, 128),
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
    resp = session.get(f"{ATLAS_BASE}{path}", params=params, timeout=30)
    if not resp.ok:
        sys.exit(f"Atlas API error {resp.status_code} on {path}: {resp.text[:500]}")
    return resp.json()


def list_clusters(session, group_id):
    data = api_get(session, f"/groups/{group_id}/clusters")
    return data.get("results", [])


def get_cluster(session, group_id, cluster_name):
    return api_get(session, f"/groups/{group_id}/clusters/{cluster_name}")


def list_processes_for_cluster(session, group_id, cluster_name):
    data = api_get(session, f"/groups/{group_id}/processes", params={"itemsPerPage": 500})
    procs = [
        p for p in data.get("results", [])
        if p.get("userAlias", "").startswith(cluster_name) or p.get("replicaSetName") == cluster_name
        or cluster_name in p.get("id", "")
    ]
    return procs


def get_measurements(session, group_id, process_id, days):
    params = {"granularity": "PT1H", "period": f"P{days}D"}
    for m in METRICS:
        params.setdefault("m", [])
    # requests needs repeated params as a list of tuples for multi-value query strings
    query = [("granularity", "PT1H"), ("period", f"P{days}D")] + [("m", m) for m in METRICS]
    resp = session.get(
        f"{ATLAS_BASE}/groups/{group_id}/processes/{process_id}/measurements",
        params=query,
        timeout=30,
    )
    if not resp.ok:
        return {}
    data = resp.json()
    out = {}
    for m in data.get("measurements", []):
        values = [dp["value"] for dp in m.get("dataPoints", []) if dp.get("value") is not None]
        out[m["name"]] = values
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


def summarize(values):
    if not values:
        return None
    return {
        "p50": round(pctl(values, 0.5), 2),
        "p95": round(pctl(values, 0.95), 2),
        "max": round(max(values), 2),
        "n": len(values),
    }


def evaluate_cluster(cluster_cfg, process_metrics, days):
    """Apply the threshold rules from references/thresholds.md. Returns (verdict, reasons, confidence)."""
    agg = {}
    for metrics in process_metrics.values():
        for name, values in metrics.items():
            agg.setdefault(name, []).extend(values)

    summary = {name: summarize(vals) for name, vals in agg.items()}

    up_reasons = []
    cpu = summary.get("SYSTEM_NORMALIZED_CPU_USER")
    if cpu and cpu["p95"] > 80:
        up_reasons.append(f"CPU p95 {cpu['p95']}% > 80% threshold")

    mem_free = summary.get("SYSTEM_MEMORY_FREE")
    mem_used = summary.get("SYSTEM_MEMORY_USED")
    if mem_free and mem_used:
        total = mem_free["p50"] + mem_used["p50"]
        if total > 0 and (mem_free["p95"] / total) < 0.10:
            up_reasons.append("Free memory p95 < 10% of total")

    for iops_name in ("DISK_PARTITION_IOPS_READ", "DISK_PARTITION_IOPS_WRITE"):
        iops = summary.get(iops_name)
        provisioned = cluster_cfg.get("_provisioned_iops")
        if iops and provisioned and iops["p95"] > 0.9 * provisioned:
            up_reasons.append(f"{iops_name} p95 {iops['p95']} > 90% of provisioned {provisioned}")

    util = summary.get("DISK_PARTITION_UTILIZATION")
    if util and util["p95"] > 90:
        up_reasons.append(f"Disk utilization p95 {util['p95']}% > 90%")

    tickets_r = summary.get("TICKETS_AVAILABLE_READS")
    tickets_w = summary.get("TICKETS_AVAILABLE_WRITES")
    for label, t in (("read", tickets_r), ("write", tickets_w)):
        if t and t["p50"] < 5:
            up_reasons.append(f"WiredTiger {label} tickets frequently near zero (p50 {t['p50']})")

    down_ok = (
        cpu and cpu["p95"] < 20
        and mem_free and mem_used and (mem_free["p50"] / max(mem_free["p50"] + mem_used["p50"], 1)) > 0.40
        and (not util or util["p95"] < 50)
        and days >= 7
    )

    if up_reasons:
        verdict = "scale_up"
        reasons = up_reasons
    elif down_ok:
        verdict = "scale_down_candidate"
        reasons = ["CPU, memory, and disk all comfortably under scale-down thresholds"]
    else:
        verdict = "no_change"
        reasons = ["No thresholds crossed; metrics are in the comfortable mid-range"]

    if days >= 7 and len(reasons) >= 1 and (not up_reasons or len(up_reasons) >= 2):
        confidence = "high"
    elif days >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return verdict, reasons, confidence, summary


def build_report(group_id, results, days):
    lines = [f"# Atlas Rightsizing Report", "",
             f"Project: `{group_id}`  |  Lookback: {days} days  |  "
             f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}Z", ""]
    for r in results:
        lines.append(f"## {r['cluster']}  —  {r['current_tier']}")
        lines.append(f"**Verdict: {r['verdict'].replace('_', ' ').upper()}**  (confidence: {r['confidence']})")
        lines.append("")
        for reason in r["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")
        lines.append("| Metric | p50 | p95 | max | samples |")
        lines.append("|---|---|---|---|---|")
        for name, s in r["metric_summary"].items():
            if s:
                lines.append(f"| {name} | {s['p50']} | {s['p95']} | {s['max']} | {s['n']} |")
        lines.append("")
    lines.append("---")
    lines.append("This is a read-only recommendation. No cluster was modified. Verify thresholds")
    lines.append("against your own risk tolerance before acting — see references/thresholds.md.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group-id", required=True, help="Atlas project (group) ID")
    ap.add_argument("--cluster", help="Cluster name; omit to audit every cluster in the project")
    ap.add_argument("--days", type=int, default=7, help="Lookback window in days (default 7)")
    ap.add_argument("--out-dir", default="./rightsizing-report", help="Output directory")
    ap.add_argument("--client-id"); ap.add_argument("--client-secret")
    ap.add_argument("--public-key"); ap.add_argument("--private-key")
    args = ap.parse_args()

    session = get_session(args)
    os.makedirs(args.out_dir, exist_ok=True)

    cluster_names = [args.cluster] if args.cluster else [
        c["name"] for c in list_clusters(session, args.group_id)
    ]

    results = []
    for name in cluster_names:
        cfg = get_cluster(session, args.group_id, name)
        tier = (
            cfg.get("replicationSpecs", [{}])[0]
            .get("regionConfigs", [{}])[0]
            .get("electableSpecs", {})
            .get("instanceSize", "UNKNOWN")
        )
        if tier in ("M0", "M2", "M5"):
            results.append({
                "cluster": name, "current_tier": tier, "verdict": "not_supported",
                "reasons": ["Free/shared tier does not expose full hardware measurements"],
                "confidence": "n/a", "metric_summary": {},
            })
            continue

        provisioned_iops = (
            cfg.get("replicationSpecs", [{}])[0]
            .get("regionConfigs", [{}])[0]
            .get("electableSpecs", {})
            .get("diskIOPS")
        )
        cfg["_provisioned_iops"] = provisioned_iops

        procs = list_processes_for_cluster(session, args.group_id, name)
        process_metrics = {}
        for p in procs:
            pid = p.get("id")
            if pid:
                process_metrics[pid] = get_measurements(session, args.group_id, pid, args.days)

        verdict, reasons, confidence, summary = evaluate_cluster(cfg, process_metrics, args.days)
        results.append({
            "cluster": name, "current_tier": tier, "verdict": verdict,
            "reasons": reasons, "confidence": confidence, "metric_summary": summary,
        })

    report_md = build_report(args.group_id, results, args.days)
    with open(os.path.join(args.out_dir, "report.md"), "w") as f:
        f.write(report_md)
    with open(os.path.join(args.out_dir, "report.json"), "w") as f:
        json.dump({"groupId": args.group_id, "days": args.days, "results": results}, f, indent=2)

    print(report_md)
    print(f"\n[written to {args.out_dir}/report.md and report.json]")


if __name__ == "__main__":
    main()
