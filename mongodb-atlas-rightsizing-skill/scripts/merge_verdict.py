#!/usr/bin/env python3
"""
Merge an Atlas hardware-metrics rightsizing report (rightsizing.py) with database-internal
diagnostics (db_diagnostics.py) into one combined report.

rightsizing.py answers "is the hardware under pressure." db_diagnostics.py answers "why, and is
more hardware actually the fix." This script combines them per the rules documented in
references/db-diagnostics.md — read that file before changing the thresholds below.

--db-diagnostics is OPTIONAL. Not every customer running this skill will have (or want to create)
database-level credentials — only an Atlas API key is required to run rightsizing.py at all. When
--db-diagnostics is omitted, this script degrades gracefully: it passes the Atlas-only verdicts
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
    "memory": "Free memory p95",
    "cache_fill": "Cache fill ratio p95",
    "connections": "Connections p95",
    "iops": "IOPS p95",
    "latency": "latency p95",
}

# Heuristic ratio for "scanned per query is suspiciously high" — not derived from official docs,
# same treatment as this project's other heuristic thresholds (page faults, disk latency). Tune
# against your own workload; a query that legitimately needs to scan many documents (an
# aggregation, a report query) will trip this without actually indicating a missing index.
HIGH_SCAN_RATIO = 10

WT_CACHE_MODERATE_PCT = 60  # below this + working set fits under cache_max => "not WT, likely OS noise"


def load_json(path):
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def fired(reasons, keyword):
    return any(keyword in r for r in reasons)


def working_set_bytes(working_set):
    """Sum data + index size across every database db_diagnostics.py inspected. Returns None if
    working_set is empty/missing/all-errored rather than a misleading 0."""
    if not working_set:
        return None
    total = 0
    saw_any = False
    for db_name, stats in working_set.items():
        if not isinstance(stats, dict) or "error" in stats:
            continue
        d = stats.get("data_size_bytes") or 0
        i = stats.get("index_size_bytes") or 0
        total += d + i
        saw_any = True
    return total if saw_any else None


def analyze_cpu(reasons, db_summary):
    if not fired(reasons, TRIGGER_KEYWORDS["cpu"]) or not db_summary:
        return None
    op_rates = db_summary.get("opcounters_per_sec", {})
    query_rate = op_rates.get("query", 0) or 0
    scanned_rate = db_summary.get("scanned_per_sec_approx", 0) or 0
    if scanned_rate <= 0:
        return None
    ratio = (scanned_rate / query_rate) if query_rate > 0 else float("inf")
    if ratio > HIGH_SCAN_RATIO:
        ratio_text = f"{ratio:.1f}x" if ratio != float("inf") else "query rate ~0 despite scanning"
        return {
            "signal": "cpu_vs_scan_efficiency",
            "note": (
                f"Atlas CPU p95 > 80% AND scanned/query ratio is {ratio_text} "
                f"(scanned {scanned_rate}/sec vs {query_rate}/sec queries) — root cause is likely "
                f"missing/poor indexes, not undersized compute. Recommend an index review "
                f"(Atlas Performance Advisor or explain()) BEFORE a tier bump — it's the cheaper "
                f"fix, and scaling up would mask the real problem while costing more every month."
            ),
            "lead_with_schema_fix": True,
        }
    return None


def analyze_memory(reasons, db_summary):
    """
    Gate primarily on working_set > wt_cache_bytes_max — that's the decisive, direct evidence
    that the cache structurally cannot hold the working set, regardless of the current instantaneous
    fill percentage. wt_cache_pct_used is corroborating detail, not a strict co-requirement: this
    project's own db_diagnostics.py run against a real M10 cluster found wt_cache_pct_used=77.5%
    (well under an initial WT_CACHE_NEAR_FULL_PCT=90% co-requirement this function used to have)
    alongside a working set 18x larger than the configured cache — a genuinely decisive, high-
    confidence scale-up case that an AND-gated "near 100%" requirement would have missed entirely.
    Caught by testing this function against real data before trusting it, not by inspection alone.
    """
    mem_or_cache_fired = fired(reasons, TRIGGER_KEYWORDS["memory"]) or fired(reasons, TRIGGER_KEYWORDS["cache_fill"])
    if not mem_or_cache_fired or not db_summary:
        return None
    wt_pct = db_summary.get("wt_cache_pct_used")
    ws_bytes = working_set_bytes(db_summary.get("_working_set_ref", {}))
    cache_max = db_summary.get("wt_cache_bytes_max")
    if wt_pct is None:
        return None
    if ws_bytes and cache_max and ws_bytes > cache_max:
        oversubscribed_x = ws_bytes / cache_max
        return {
            "signal": "memory_confirmed",
            "note": (
                f"Atlas memory/cache-fill signal AND db-internal working set ({ws_bytes:,} bytes) "
                f"exceeds the configured WT cache ({cache_max:,} bytes) by {oversubscribed_x:.1f}x "
                f"(current cache fill: {wt_pct}%) — working set genuinely doesn't fit in RAM at "
                f"the current tier. This is a real memory-driven scale-up case, not noise. "
                f"Confidence: HIGH."
            ),
            "confidence_override": "high",
        }
    elif ws_bytes and cache_max and ws_bytes <= cache_max and wt_pct < WT_CACHE_MODERATE_PCT:
        return {
            "signal": "memory_unconfirmed",
            "note": (
                f"Atlas flagged low free memory / cache pressure, but db-internal "
                f"wt_cache_pct_used={wt_pct}% is moderate and the working set ({ws_bytes:,} bytes) "
                f"fits under the configured WT cache ({cache_max:,} bytes). The OS-level memory "
                f"pressure Atlas is seeing likely isn't coming from WiredTiger — could be another "
                f"process, normal OS page-cache behavior, or a short spike. LOWER confidence on a "
                f"memory-driven scale-up; don't recommend one from the Atlas signal alone."
            ),
            "confidence_override": "low",
        }
    return None


def analyze_connections(reasons, db_summary):
    atlas_fired = fired(reasons, TRIGGER_KEYWORDS["connections"])
    if not db_summary:
        return None
    db_pct = db_summary.get("connections_pct_used")
    if db_pct is None:
        return None
    db_near_limit = db_pct > 60
    if atlas_fired and db_near_limit:
        return {
            "signal": "connections_agree",
            "note": (
                f"Atlas connections trigger AND db-internal connections_pct_used={db_pct}% agree "
                f"— real connection pressure, likely a connection-pooling problem in the "
                f"application as much as a tier problem. Worth mentioning both fixes."
            ),
        }
    if atlas_fired and not db_near_limit:
        return {
            "signal": "connections_disagree",
            "note": (
                f"DISAGREEMENT: Atlas flagged connections pressure, but db-internal "
                f"connections_pct_used is only {db_pct}% right now. Possible causes: different "
                f"sampling windows, or a spike that's since resolved. Showing both numbers rather "
                f"than picking one — don't treat this as a confirmed connections problem without "
                f"checking the Atlas connections chart for when the spike occurred."
            ),
        }
    if not atlas_fired and db_near_limit:
        return {
            "signal": "connections_disagree",
            "note": (
                f"DISAGREEMENT: db-internal connections_pct_used={db_pct}% is elevated right now, "
                f"but Atlas's 7-day p95 didn't cross the trigger. This snapshot may be catching a "
                f"spike the wider Atlas window smoothed out — worth a closer look at recent "
                f"connection counts rather than dismissing it."
            ),
        }
    return None


def analyze_disk(reasons, db_summary):
    disk_fired = fired(reasons, TRIGGER_KEYWORDS["iops"]) or fired(reasons, TRIGGER_KEYWORDS["latency"])
    if not disk_fired or not db_summary:
        return None
    read_into_rate = db_summary.get("wt_pages_read_into_cache_per_sec")
    if read_into_rate and read_into_rate > 0:
        return {
            "signal": "disk_cache_miss_driven",
            "note": (
                f"Atlas disk IOPS/latency signal AND db-internal "
                f"wt_pages_read_into_cache_per_sec={read_into_rate}/sec — cache misses are driving "
                f"disk reads. This points at working-set-vs-cache-size (see memory analysis above "
                f"if present) rather than pure write volume; reinforces or explains the disk signal."
            ),
        }
    return None


def analyze_result(result, db_diagnostics):
    """Run all combination rules for one rightsizing result (one cluster or shard)."""
    reasons = result.get("reasons", [])
    db_summary = None
    if db_diagnostics:
        db_summary = dict(db_diagnostics.get("server_status_summary", {}))
        db_summary["_working_set_ref"] = db_diagnostics.get("working_set", {})

    notes = []
    for analyzer in (analyze_cpu, analyze_memory, analyze_connections, analyze_disk):
        finding = analyzer(reasons, db_summary)
        if finding:
            notes.append(finding)
    return notes


def build_merged_report(rightsizing_report, db_diagnostics, db_diagnostics_path):
    lines = ["# Merged Rightsizing + DB Diagnostics Report", ""]
    lines.append(f"Project: `{rightsizing_report.get('groupId')}`  |  "
                 f"Lookback: {rightsizing_report.get('windowLabel', rightsizing_report.get('windowDays'))}  |  "
                 f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}Z")
    lines.append("")

    results = rightsizing_report.get("results", [])
    multi_shard = len([r for r in results if r.get("verdict") != "not_applicable"]) > 1

    if not db_diagnostics:
        lines.append("**Mode: ATLAS-ONLY** — no `--db-diagnostics` file provided. The verdicts "
                      "below are exactly what `rightsizing.py` produced from Atlas hardware "
                      "metrics alone. If you have (or can get) a MongoDB database user with "
                      "`clusterMonitor` and read access, run `db_diagnostics.py` and re-run this "
                      "merge for root-cause analysis (e.g. distinguishing \"add an index\" from "
                      "\"buy a bigger cluster\") — see references/db-diagnostics.md.")
        lines.append("")
    else:
        lines.append(f"**Mode: COMBINED** — merged with `{db_diagnostics_path}` "
                      f"(sampled {db_diagnostics.get('sampled_at', 'unknown time')}).")
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
        for reason in r.get("reasons", []):
            lines.append(f"- {reason}")
        lines.append("")

        if db_diagnostics:
            combined_notes = analyze_result(r, db_diagnostics)
            if combined_notes:
                lines.append("**Combined analysis (Atlas + DB-internal):**")
                lines.append("")
                for note in combined_notes:
                    prefix = "[DISAGREEMENT] " if "DISAGREEMENT" in note["note"] else "[AGREES] "
                    lines.append(f"{prefix}{note['note']}")
                    lines.append("")
            elif r.get("verdict") == "scale_up":
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

    rightsizing_report = load_json(args.rightsizing_report)
    if rightsizing_report is None:
        raise SystemExit(f"Could not read rightsizing report: {args.rightsizing_report}")

    db_diagnostics = load_json(args.db_diagnostics) if args.db_diagnostics else None
    if args.db_diagnostics and db_diagnostics is None:
        print(f"WARNING: --db-diagnostics path given but not found/readable: {args.db_diagnostics} "
              f"— continuing in Atlas-only mode.")

    merged_md = build_merged_report(rightsizing_report, db_diagnostics, args.db_diagnostics)

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "merged_report.md"), "w", encoding="utf-8") as f:
        f.write(merged_md)
    with open(os.path.join(args.out_dir, "merged_report.json"), "w", encoding="utf-8") as f:
        json.dump({
            "groupId": rightsizing_report.get("groupId"),
            "mode": "combined" if db_diagnostics else "atlas_only",
            "results": [
                {**r, "combined_analysis": analyze_result(r, db_diagnostics) if db_diagnostics else []}
                for r in rightsizing_report.get("results", [])
            ],
        }, f, indent=2)

    print(merged_md)
    print(f"\n[written to {args.out_dir}/merged_report.md and merged_report.json]")


if __name__ == "__main__":
    main()
