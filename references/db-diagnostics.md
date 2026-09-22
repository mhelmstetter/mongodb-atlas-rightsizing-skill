# Combining Atlas hardware metrics with database-internal diagnostics

`rightsizing.py` answers "is the hardware under pressure." `db_diagnostics.py` answers "why, and
is more hardware actually the fix." Run both when you have database credentials available —
Atlas metrics alone can only ever recommend bigger/smaller; db diagnostics let the skill
recommend *index this collection* or *shard this* instead of a tier change, which is often the
cheaper and correct fix.

`scripts/merge_verdict.py` implements the rules on this page as executable code — the table below
and `TRIGGER_KEYWORDS`/`HIGH_SCAN_RATIO`/`WT_CACHE_MODERATE_PCT` in that script should stay in
sync.

**Total data size is not the working set.** `db_diagnostics.py` reports `data_footprint`
(`dataSize` + `indexSize` per database) — the total data, an upper bound on the working set. Only
the hot subset has to fit in cache. So the comparison with `wt_cache_bytes_max` is decisive in one
direction only: if *all* data fits, the WiredTiger cache can't be the source of memory pressure.
If total data exceeds the cache, that's context, not confirmation — a real M10 had total data 18x
the cache while cache fill p95 was 78% and page faults p95 0.32/s, which points to a hot set that
fits.

Each combined finding is labeled `[AGREES]`, `[DISAGREES]`, or `[CONTEXT]`. Only results with an
evaluated verdict (`scale_up`, `change_disk_or_iops`, `scale_down_candidate`, `no_change`) get
combined analysis — not routers, paused/unsupported clusters, or `insufficient_data`.

## Signals and what they mean together

| Atlas signal | DB-internal signal | Combined read |
|---|---|---|
| CPU p95 > 80% | `scanned_objects_per_sec` / `docs_returned_per_sec` > `HIGH_SCAN_RATIO` (documents examined per document returned) | Root cause is likely missing/poor indexes, not undersized compute. Recommend an index review (Atlas Performance Advisor or `explain()`) before a tier bump. (Index keys scanned and `opcounters.query` aren't used: key scans are normal for indexed range queries, and `opcounters.query` leaves out aggregations.) |
| Free memory low / cache fill high | Total data + indexes exceeds `wt_cache_bytes_max` | Context only: total data is an upper bound on the working set. Check the Atlas page-fault and cache-read rows and `wt_pages_read_into_cache_per_sec` before calling it memory-driven. |
| Free memory low, but... | All data + indexes fit under `wt_cache_bytes_max` and `wt_cache_pct_used` is moderate | Disagreement: the OS-level memory pressure isn't coming from WiredTiger — another process, the OS page cache doing its normal thing, or a short spike. Don't recommend a memory-driven scale-up from this signal alone. |
| Connections p95 near limit | `connections_pct_used` from serverStatus agrees | Corroborated — real connection pressure, likely a connection-pooling problem in the app as much as a tier problem. Worth mentioning both fixes. |
| Connections p95 near limit | `connections_pct_used` is low | Disagreement — the Atlas-side number may reflect a different sampling window or a spike that's since resolved. Note the discrepancy in the report rather than picking one silently. |
| Disk IOPS/latency high | `wt_pages_read_into_cache_per_sec` | Context only (any active cluster reads pages into cache): if it's a large share of read IOPS, cache misses are driving disk reads rather than write volume. |

## Rule: disagreement gets surfaced, not silently resolved

If Atlas says one thing and db diagnostics say another (see the connections example above), the
report should show both numbers and say so explicitly, with a plausible reason (different time
windows, a resolved spike, sampling interval too short) — never quietly prefer one source. The
user needs to know the picture is mixed, not get a false sense of certainty.

## Rule: schema/query fixes are cheaper — surface them first

When a scan-efficiency or index problem is detected alongside a hardware-pressure signal, lead
the recommendation with the schema/query fix and frame the tier change as the fallback if that
doesn't resolve it. Scaling up masks an indexing problem and costs money every month; fixing the
index is often a one-time change that removes the pressure entirely.

## Practical notes on running `db_diagnostics.py`

- `serverStatus()` counters are cumulative since mongod start, so the script takes two samples
  `--sample-interval` seconds apart to compute rates. Use at least 60s; longer (300s+) gives a
  cleaner rate if the workload is bursty. This makes the script take that long to run — mention
  this to the user before kicking it off.
- Requires a user with `clusterMonitor` (for `serverStatus`) and `read` on the databases being
  inspected (for `dbStats`/`collStats`). Don't ask for more than that. A database it can't read
  is an error, not a skipped entry — pass `--databases` to limit the scope.
- Pass the connection string in `MONGODB_URI` rather than `--uri`, so the password stays out of
  shell history and the process list.
- Connecting through `mongos` gives no WiredTiger data (mongos has no storage engine); the
  memory comparison then reports that instead of running.
- On a sharded cluster, connect to `mongos` for cluster-wide dbStats, but also consider running
  `serverStatus` against individual shard primaries if a specific shard is suspected — a
  mongos-level view averages away a single hot shard the same way cluster-wide Atlas metrics can.
- This complements `rightsizing.py`; it doesn't replace it. Hardware metrics still matter for
  raw CPU/IOPS ceiling questions that db-internal stats don't see directly.
