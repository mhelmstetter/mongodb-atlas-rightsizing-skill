#!/usr/bin/env python3
"""
Database-internal diagnostics to complement the Atlas Admin API hardware metrics.

Connects directly to a MongoDB deployment (via connection string — requires a user with at
least the `clusterMonitor` role, or `read`/`readAnyDatabase` for the collStats/dbStats parts)
and pulls:

  - serverStatus(): connection headroom, WiredTiger cache pressure, query-executor scan
    efficiency, opcounter rates (sampled twice to compute rates from cumulative counters)
  - dbStats() / collStats() per database: real dataSize + indexSize, to estimate working set

This is a DIFFERENT data source from rightsizing.py (which hits the Atlas Admin API for OS/
hardware measurements). Run both and pass this script's --out-dir into
scripts/merge_verdict.py to combine them — see references/db-diagnostics.md for why they're
kept separate and how the merge logic reasons about disagreements between the two.

This script only runs read-only admin commands. It never writes to the database.

Usage:
  MONGODB_URI="mongodb+srv://user:pass@cluster.../admin" python db_diagnostics.py \\
      --databases mydb another_db --sample-interval 60 --out-dir ./report

  Omit --databases to auto-discover all non-system databases.

Only dependency: `pymongo` (pip install pymongo).
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    from pymongo import MongoClient
except ImportError:
    sys.exit("This script requires pymongo: pip install pymongo")


def get_server_status(client):
    return client.admin.command("serverStatus")


def summarize_server_status(before, after, elapsed_s):
    """Turn two point-in-time serverStatus snapshots into rates + pressure signals.

    Counters are read by key, not with defaults: a missing counter is an error, not a zero rate
    that would read as "no activity"."""
    def rate(path):
        b, a = before, after
        for key in path:
            b, a = b[key], a[key]
        return round((a - b) / elapsed_s, 2)

    op_rates = {k: rate(("opcounters", k)) for k in after["opcounters"]}
    conns = after["connections"]

    summary = {
        "process": after["process"],
        "host": after["host"],
        "connections_current": conns["current"],
        "connections_available": conns["available"],
        "connections_pct_used": round(100 * conns["current"] / (conns["current"] + conns["available"]), 1),
        "opcounters_per_sec": op_rates,
        # Documents examined vs documents returned: a high ratio is the collection-scan /
        # poor-index signal. (queryExecutor.scanned counts index keys examined, which is normal for
        # indexed range queries, and opcounters.query leaves out aggregations.)
        "scanned_objects_per_sec": rate(("metrics", "queryExecutor", "scannedObjects")),
        "scanned_keys_per_sec": rate(("metrics", "queryExecutor", "scanned")),
        "docs_returned_per_sec": rate(("metrics", "document", "returned")),
        "resident_mem_mb": after["mem"]["resident"],
    }

    # mongos has no storage engine, so no WiredTiger section. Recorded as unavailable rather than
    # left out, so merge_verdict.py can say why the cache comparison didn't run.
    if after["process"] == "mongos":
        summary["wt_available"] = False
        return summary

    cache = after["wiredTiger"]["cache"]
    summary.update({
        "wt_available": True,
        "wt_cache_bytes_used": cache["bytes currently in the cache"],
        "wt_cache_bytes_max": cache["maximum bytes configured"],
        "wt_cache_pct_used": round(100 * cache["bytes currently in the cache"]
                                   / cache["maximum bytes configured"], 1),
        "wt_pages_evicted_per_sec": round(
            rate(("wiredTiger", "cache", "unmodified pages evicted"))
            + rate(("wiredTiger", "cache", "modified pages evicted")), 2),
        "wt_pages_read_into_cache_per_sec": rate(("wiredTiger", "cache", "pages read into cache")),
        "page_faults_total": after["extra_info"]["page_faults"],
    })
    return summary


def get_data_footprint(client, database_names):
    """dataSize + indexSize per database (plus the largest collections). This is the total data
    footprint — an upper bound on the working set, not the working set itself: only the hot
    subset of it needs to fit in cache."""
    result = {}
    for dbname in database_names:
        db = client[dbname]
        dbstats = db.command("dbStats")
        colls = []
        # type=collection leaves out views (collStats fails on them).
        for coll_name in db.list_collection_names(filter={"type": "collection"}):
            cs = db.command("collStats", coll_name)
            colls.append({
                "name": coll_name,
                "count": cs["count"],
                "size_bytes": cs["size"],
                "storage_size_bytes": cs["storageSize"],
                "total_index_size_bytes": cs["totalIndexSize"],
            })
        colls.sort(key=lambda c: c["size_bytes"] + c["total_index_size_bytes"], reverse=True)
        result[dbname] = {
            "data_size_bytes": dbstats["dataSize"],
            "index_size_bytes": dbstats["indexSize"],
            "storage_size_bytes": dbstats["storageSize"],
            "collections": len(colls),
            "top_collections_by_footprint": colls[:10],
        }
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", help="MongoDB connection string (mongodb+srv://...). Prefer the "
                                   "MONGODB_URI env var so the password stays out of shell history "
                                   "and the process list.")
    ap.add_argument("--databases", nargs="*", help="Databases to inspect; omit to auto-discover")
    ap.add_argument("--sample-interval", type=int, default=60,
                     help="Seconds between the two serverStatus samples used to compute rates (default 60)")
    ap.add_argument("--out-dir", default="./rightsizing-report")
    args = ap.parse_args()
    uri = args.uri or os.environ.get("MONGODB_URI")
    if not uri:
        ap.error("provide the connection string via MONGODB_URI (preferred) or --uri")
    if args.sample_interval < 1:
        ap.error("--sample-interval must be at least 1 second")

    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")  # fail fast on bad creds/network before waiting a full interval

    databases = args.databases or [
        d for d in client.list_database_names() if d not in ("admin", "local", "config")
    ]

    print(f"Sampling serverStatus twice, {args.sample_interval}s apart, to compute rates...")
    before = get_server_status(client)
    time.sleep(args.sample_interval)
    after = get_server_status(client)
    # Server-side elapsed time; also catches a restart between samples, which resets every counter.
    elapsed = (after["uptimeMillis"] - before["uptimeMillis"]) / 1000
    if after["host"] != before["host"] or elapsed <= 0:
        sys.exit(f"The two serverStatus samples came from different processes or across a restart "
                 f"({before['host']} -> {after['host']}, elapsed {elapsed}s); rates can't be computed.")

    diagnostics = {
        "sampled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sample_interval_s": round(elapsed, 1),
        "server_status_summary": summarize_server_status(before, after, elapsed),
        "data_footprint": get_data_footprint(client, databases),
    }

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, "db_diagnostics.json")
    with open(out_path, "w") as f:
        json.dump(diagnostics, f, indent=2)

    print(json.dumps(diagnostics, indent=2))
    print(f"\n[written to {out_path}]")


if __name__ == "__main__":
    main()
