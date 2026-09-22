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
  python db_diagnostics.py --uri "mongodb+srv://user:pass@cluster.../admin" \\
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
    """Turn two point-in-time serverStatus snapshots into rates + pressure signals."""
    wt_before = before.get("wiredTiger", {}).get("cache", {})
    wt_after = after.get("wiredTiger", {}).get("cache", {})

    def rate(path_fn):
        try:
            b = path_fn(before)
            a = path_fn(after)
            return round((a - b) / elapsed_s, 2) if elapsed_s > 0 else None
        except (KeyError, TypeError):
            return None

    opcounters = after.get("opcounters", {})
    opcounters_before = before.get("opcounters", {})
    op_rates = {
        k: round((opcounters.get(k, 0) - opcounters_before.get(k, 0)) / max(elapsed_s, 1), 1)
        for k in opcounters
    }

    qe = after.get("metrics", {}).get("queryExecutor", {})
    qe_before = before.get("metrics", {}).get("queryExecutor", {})
    scanned_delta = qe.get("scanned", {}).get("total", 0) - qe_before.get("scanned", {}).get("total", 0) \
        if isinstance(qe.get("scanned"), dict) else (qe.get("scanned", 0) - qe_before.get("scanned", 0))
    returned_delta = op_rates.get("query", 0) * elapsed_s  # rough proxy

    cache_bytes = wt_after.get("bytes currently in the cache")
    cache_max = wt_after.get("maximum bytes configured")
    cache_pct = round(100 * cache_bytes / cache_max, 1) if cache_bytes and cache_max else None

    evicted_rate = rate(lambda s: s.get("wiredTiger", {}).get("cache", {}).get("pages evicted", 0))
    read_into_cache_rate = rate(
        lambda s: s.get("wiredTiger", {}).get("cache", {}).get("pages read into cache", 0)
    )

    conns = after.get("connections", {})

    return {
        "connections_current": conns.get("current"),
        "connections_available": conns.get("available"),
        "connections_pct_used": round(
            100 * conns.get("current", 0) / (conns.get("current", 0) + conns.get("available", 1)), 1
        ),
        "wt_cache_bytes_used": cache_bytes,
        "wt_cache_bytes_max": cache_max,
        "wt_cache_pct_used": cache_pct,
        "wt_pages_evicted_per_sec": evicted_rate,
        "wt_pages_read_into_cache_per_sec": read_into_cache_rate,
        "opcounters_per_sec": op_rates,
        "scanned_per_sec_approx": round(scanned_delta / max(elapsed_s, 1), 1),
        "resident_mem_mb": after.get("mem", {}).get("resident"),
        "page_faults_total": after.get("extra_info", {}).get("page_faults"),
    }


def get_working_set(client, database_names):
    result = {}
    for dbname in database_names:
        db = client[dbname]
        try:
            dbstats = db.command("dbStats")
        except Exception as e:
            result[dbname] = {"error": str(e)}
            continue
        colls = []
        for coll_name in db.list_collection_names():
            try:
                cs = db.command("collStats", coll_name)
                colls.append({
                    "name": coll_name,
                    "count": cs.get("count"),
                    "size_bytes": cs.get("size"),
                    "storage_size_bytes": cs.get("storageSize"),
                    "total_index_size_bytes": cs.get("totalIndexSize"),
                })
            except Exception:
                continue
        colls.sort(key=lambda c: (c.get("size_bytes") or 0) + (c.get("total_index_size_bytes") or 0), reverse=True)
        result[dbname] = {
            "data_size_bytes": dbstats.get("dataSize"),
            "index_size_bytes": dbstats.get("indexSize"),
            "storage_size_bytes": dbstats.get("storageSize"),
            "collections": len(colls),
            "top_collections_by_footprint": colls[:10],
        }
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", required=True, help="MongoDB connection string (mongodb+srv://...)")
    ap.add_argument("--databases", nargs="*", help="Databases to inspect; omit to auto-discover")
    ap.add_argument("--sample-interval", type=int, default=60,
                     help="Seconds between the two serverStatus samples used to compute rates (default 60)")
    ap.add_argument("--out-dir", default="./rightsizing-report")
    args = ap.parse_args()

    client = MongoClient(args.uri, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")  # fail fast on bad creds/network before waiting a full interval

    databases = args.databases or [
        d for d in client.list_database_names() if d not in ("admin", "local", "config")
    ]

    print(f"Sampling serverStatus twice, {args.sample_interval}s apart, to compute rates...")
    before = get_server_status(client)
    t0 = time.time()
    time.sleep(args.sample_interval)
    after = get_server_status(client)
    elapsed = time.time() - t0

    diagnostics = {
        "sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "sample_interval_s": round(elapsed, 1),
        "server_status_summary": summarize_server_status(before, after, elapsed),
        "working_set": get_working_set(client, databases),
    }

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, "db_diagnostics.json")
    with open(out_path, "w") as f:
        json.dump(diagnostics, f, indent=2)

    print(json.dumps(diagnostics, indent=2))
    print(f"\n[written to {out_path}]")


if __name__ == "__main__":
    main()
