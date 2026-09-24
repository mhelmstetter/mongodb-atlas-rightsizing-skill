#!/usr/bin/env python3
"""
Merge an Atlas hardware-metrics rightsizing report (rightsizing.py) with database-internal
diagnostics (db_diagnostics.py) into one combined report.

rightsizing.py answers "is the hardware under pressure." db_diagnostics.py answers "why, and is
more hardware actually the fix." This script combines them per the rules documented in
references/db-diagnostics.md — read that file before changing the thresholds below.

--db-diagnostics is OPTIONAL. Not every customer running this skill will have (or want to create)
database-level credentials — only an Atlas API key is required to run rightsizing.py at all. When
--db-diagnostics is omitted, it passes the Atlas-only verdicts
through unchanged, clearly labeled as Atlas-only, with a note that db_diagnostics.py is available
for deeper root-cause analysis if DB credentials become available later.

Core rule (see references/db-diagnostics.md): when the two sources disagree, SURFACE the
disagreement — never silently prefer one number over the other.

This script is READ-ONLY and makes no network calls; it only reads the two JSON files.

Usage:
  python merge_verdict.py --rightsizing-report ./report/report.json \\
      --db-diagnostics ./report/db_diagnostics.json --out-dir ./report

  # Atlas-only (no DB credentials available) — still produces a valid, clearly-labeled report:
  python merge_verdict.py --rightsizing-report ./report/report.json --out-dir ./report
"""

import argparse
import json
import os
from datetime import datetime, timezone

# Must match the wording rightsizing.py's evaluate_cluster() actually generates in `reasons` —
# report.json doesn't expose which internal thresholds fired as structured data, only the
# human-readable reason strings, so this is deliberately a same-repo, same-author keyword match
# against wording this script's own sibling controls (not a robust interface to depend on if
# rightsizing.py's reason text changes without updating this list to match).
TRIGGER_KEYWORDS = {
    "cpu": "CPU p95",
    "memory": "Available memory p5",
    "cache_fill": "Cache fill ratio p95",
    "connections": "Connections p95",
    "iops": "IOPS p95",
    "latency": "latency p95",
}

# Heuristic: documents examined per document returned. Not derived from official docs, same
# treatment as this project's other heuristic thresholds (page faults, disk latency). A workload
# that legitimately examines many documents per result (aggregations with $group, reports) trips
# this without a missing index.
HIGH_SCAN_RATIO = 10

WT_CACHE_MODERATE_PCT = 60  # below this + all data fits under cache_max => pressure isn't from WT

# Verdicts that come from an evaluation of the hardware metrics; the others (paused,
# not_supported, not_applicable for routers, insufficient_data) have nothing to corroborate.
EVALUATED_VERDICTS = ("scale_up", "change_disk_or_iops", "scale_down_candidate", "no_change")

# Finding kinds, rendered as the [PREFIX] in the report.
AGREES, DISAGREES, CONTEXT = "agrees", "disagrees", "context"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def fired(reasons, keyword):
    return any(keyword in r for r in reasons)


def total_data_bytes(data_footprint):
    """Sum of dataSize + indexSize across every database db_diagnostics.py inspected — the total
    data footprint, an upper bound on the working set."""
    return sum(d["data_size_bytes"] + d["index_size_bytes"] for d in data_footprint.values())


def analyze_cpu(reasons, db, window_label):
    if not fired(reasons, TRIGGER_KEYWORDS["cpu"]):
        return None
    examined = db["scanned_objects_per_sec"]
    returned = db["docs_returned_per_sec"]
    if examined <= 0:
        return None
    ratio = examined / returned if returned > 0 else float("inf")
    if ratio <= HIGH_SCAN_RATIO:
        return None
    ratio_text = f"{ratio:.1f}x" if returned > 0 else "documents examined with ~0 returned"
    return {
        "signal": "cpu_vs_scan_efficiency",
        "kind": AGREES,
        "note": (
            f"Atlas CPU trigger AND documents examined per document returned is {ratio_text} "
            f"({examined}/sec examined vs {returned}/sec returned) — likely missing or poor "
            f"indexes rather than undersized compute. Recommend an index review (Atlas "
            f"Performance Advisor or explain()) BEFORE a tier bump — it's the cheaper fix, and "
            f"scaling up would mask the real problem while costing more every month."
        ),
    }


def analyze_memory(reasons, db, data_footprint, window_label):
    """Total data size vs configured cache. All data fitting in cache is decisive (the cache can't
    be the pressure); data exceeding the cache is not — only the hot subset has to fit, so that
    case is reported as context with the cache read-in rate, not as confirmation."""
    if not (fired(reasons, TRIGGER_KEYWORDS["memory"]) or fired(reasons, TRIGGER_KEYWORDS["cache_fill"])):
        return None
    if not db["wt_available"]:
        return {
            "signal": "memory_no_wt_data",
            "kind": CONTEXT,
            "note": (f"Atlas flagged memory/cache pressure, but db_diagnostics.py connected to "
                     f"{db['process']} ({db['host']}), which has no WiredTiger cache — run it "
                     f"against a shard member to compare data size with cache size."),
        }
    data_bytes = total_data_bytes(data_footprint)
    cache_max = db["wt_cache_bytes_max"]
    wt_pct = db["wt_cache_pct_used"]
    if data_bytes <= cache_max and wt_pct < WT_CACHE_MODERATE_PCT:
        return {
            "signal": "memory_unconfirmed",
            "kind": DISAGREES,
            "note": (
                f"Atlas flagged low free memory / cache pressure, but ALL data + indexes "
                f"({data_bytes:,} bytes) fit in the configured WT cache ({cache_max:,} bytes) and "
                f"wt_cache_pct_used={wt_pct}%. The OS-level memory pressure likely isn't coming "
                f"from WiredTiger — another process, normal OS page-cache behavior, or a short "
                f"spike. Don't recommend a memory-driven scale-up from the Atlas signal alone."
            ),
        }
    if data_bytes > cache_max:
        return {
            "signal": "memory_data_exceeds_cache",
            "kind": CONTEXT,
            "note": (
                f"Total data + indexes ({data_bytes:,} bytes) is {data_bytes / cache_max:.1f}x the "
                f"configured WT cache ({cache_max:,} bytes); cache fill {wt_pct}%, "
                f"{db['wt_pages_read_into_cache_per_sec']} pages/sec read into cache during the "
                f"sample. That's an upper bound on the working set, not the working set — it "
                f"only confirms memory pressure if pages are being read in continuously. Compare "
                f"with the page-fault and cache-read rows in the Atlas table before deciding."
            ),
        }
    return None


def analyze_connections(reasons, db, window_label):
    atlas_fired = fired(reasons, TRIGGER_KEYWORDS["connections"])
    db_pct = db["connections_pct_used"]
    db_near_limit = db_pct > 60
    if atlas_fired and db_near_limit:
        return {
            "signal": "connections_agree",
            "kind": AGREES,
            "note": (
                f"Atlas connections trigger AND db-internal connections_pct_used={db_pct}% agree "
                f"— real connection pressure, likely a connection-pooling problem in the "
                f"application as much as a tier problem. Worth mentioning both fixes."
            ),
        }
    if atlas_fired and not db_near_limit:
        return {
            "signal": "connections_disagree",
            "kind": DISAGREES,
            "note": (
                f"Atlas flagged connections pressure over {window_label}, but db-internal "
                f"connections_pct_used is only {db_pct}% right now. Possible causes: different "
                f"sampling windows, or a spike that's since resolved. Showing both numbers rather "
                f"than picking one — check the Atlas connections chart for when the spike occurred."
            ),
        }
    if not atlas_fired and db_near_limit:
        return {
            "signal": "connections_disagree",
            "kind": DISAGREES,
            "note": (
                f"db-internal connections_pct_used={db_pct}% is elevated right now, but Atlas's "
                f"p95 over {window_label} didn't cross the trigger. This snapshot may be catching "
                f"a spike the wider Atlas window smoothed out — worth a closer look at recent "
                f"connection counts rather than dismissing it."
            ),
        }
    return None


def analyze_disk(reasons, db, window_label):
    """Context only: any active cluster reads pages into cache, so a nonzero rate doesn't by
    itself show cache misses are driving the disk signal."""
    if not (fired(reasons, TRIGGER_KEYWORDS["iops"]) or fired(reasons, TRIGGER_KEYWORDS["latency"])):
        return None
    if not db["wt_available"]:
        return None
    return {
        "signal": "disk_cache_read_in",
        "kind": CONTEXT,
        "note": (
            f"Atlas disk IOPS/latency signal; db-internal wt_pages_read_into_cache_per_sec="
            f"{db['wt_pages_read_into_cache_per_sec']}/sec during the sample. If that's a large "
            f"share of read IOPS, cache misses (working set vs cache size) are driving disk reads "
            f"rather than write volume — see the memory analysis if present."
        ),
    }


def analyze_result(result, db_diagnostics, window_label):
    """Run all combination rules for one rightsizing result (one cluster or shard)."""
    # db_diagnostics.py connects to mongod/mongos, so it says nothing about search nodes.
    if result["component"] != "mongod" or result["verdict"] not in EVALUATED_VERDICTS:
        return []
    reasons = result["reasons"]
    db = db_diagnostics["server_status_summary"]
    findings = [
        analyze_cpu(reasons, db, window_label),
        analyze_memory(reasons, db, db_diagnostics["data_footprint"], window_label),
        analyze_connections(reasons, db, window_label),
        analyze_disk(reasons, db, window_label),
    ]
    return [f for f in findings if f]


def build_merged_report(rightsizing_report, db_diagnostics, db_diagnostics_path):
    window_label = rightsizing_report["windowLabel"]
    lines = ["# Merged Rightsizing + DB Diagnostics Report", ""]
    lines.append(f"Project: `{rightsizing_report['groupId']}`  |  "
                 f"Lookback: {window_label}  |  "
                 f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("")

    results = rightsizing_report["results"]
    multi_shard = len([r for r in results if r["component"] == "mongod"
                       and r["verdict"] in EVALUATED_VERDICTS]) > 1

    if not db_diagnostics:
        lines.append("**Mode: ATLAS-ONLY** — no `--db-diagnostics` file provided. The verdicts "
                      "below are exactly what `rightsizing.py` produced from Atlas hardware "
                      "metrics alone. If you have (or can get) a MongoDB database user with "
                      "`clusterMonitor` and read access, run `db_diagnostics.py` and re-run this "
                      "merge for root-cause analysis (e.g. distinguishing \"add an index\" from "
                      "\"buy a bigger cluster\") — see references/db-diagnostics.md.")
        lines.append("")
    else:
        db = db_diagnostics["server_status_summary"]
        lines.append(f"**Mode: COMBINED** — merged with `{db_diagnostics_path}` "
                      f"(sampled {db_diagnostics['sampled_at']} from {db['process']} on "
                      f"`{db['host']}`). Nothing ties that file to this report's cluster — "
                      f"make sure it was run against the same one.")
        if multi_shard:
            lines.append("")
            lines.append("**WARNING:** This rightsizing report covers **more than one shard/replica set**, "
                          "but db_diagnostics.py connects to a single host/mongos. The "
                          "db-internal signals below reflect only that one connection point and "
                          "may not represent every shard — per references/db-diagnostics.md, a "
                          "mongos-level or single-shard view can average away (or miss entirely) "
                          "a hot shard. Re-run db_diagnostics.py against other shard primaries if "
                          "a specific shard is suspected.")
        lines.append("")

    for r in results:
        lines.append(f"## {r['cluster']}  —  {r['current_tier']}")
        lines.append(f"**Atlas verdict: {r['verdict'].replace('_', ' ').upper()}**  "
                      f"(confidence: {r['confidence']})")
        lines.append("")
        for reason in r["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

        if db_diagnostics:
            combined_notes = analyze_result(r, db_diagnostics, window_label)
            if combined_notes:
                lines.append("**Combined analysis (Atlas + DB-internal):**")
                lines.append("")
                for note in combined_notes:
                    lines.append(f"[{note['kind'].upper()}] {note['note']}")
                    lines.append("")
            elif r["verdict"] in ("scale_up", "change_disk_or_iops"):
                lines.append("*No db-internal signal corroborated or contradicted this verdict — "
                              "the Atlas-only reasoning above stands on its own.*")
                lines.append("")

    lines.append("---")
    lines.append("This is a read-only recommendation. No cluster was modified.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rightsizing-report", required=True,
                     help="Path to rightsizing.py's report.json")
    ap.add_argument("--db-diagnostics", default=None,
                     help="Path to db_diagnostics.py's db_diagnostics.json (optional — omit for "
                          "Atlas-only customers with no database credentials)")
    ap.add_argument("--out-dir", default="./rightsizing-report")
    args = ap.parse_args()

    for path in (args.rightsizing_report, args.db_diagnostics):
        if path and not os.path.isfile(path):
            ap.error(f"file not found: {path}")
    rightsizing_report = load_json(args.rightsizing_report)
    # Atlas-only mode is chosen by omitting --db-diagnostics, never by a path that can't be read.
    db_diagnostics = load_json(args.db_diagnostics) if args.db_diagnostics else None

    merged_md = build_merged_report(rightsizing_report, db_diagnostics, args.db_diagnostics)

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "merged_report.md"), "w", encoding="utf-8") as f:
        f.write(merged_md)
    with open(os.path.join(args.out_dir, "merged_report.json"), "w", encoding="utf-8") as f:
        json.dump({
            "groupId": rightsizing_report["groupId"],
            "mode": "combined" if db_diagnostics else "atlas_only",
            "results": [
                {**r, "combined_analysis": (analyze_result(r, db_diagnostics, rightsizing_report["windowLabel"])
                                            if db_diagnostics else [])}
                for r in rightsizing_report["results"]
            ],
        }, f, indent=2)

    print(merged_md)
    print(f"\n[written to {args.out_dir}/merged_report.md and merged_report.json]")


if __name__ == "__main__":
    main()
