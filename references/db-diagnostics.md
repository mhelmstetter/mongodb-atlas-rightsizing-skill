# Combining Atlas hardware metrics with database-internal diagnostics

`rightsizing.py` answers "is the hardware under pressure." `db_diagnostics.py` answers "why, and
is more hardware actually the fix." Run both when you have database credentials available —
Atlas metrics alone can only ever recommend bigger/smaller; db diagnostics let the skill
recommend *index this collection* or *shard this* instead of a tier change, which is often the
cheaper and correct fix.

## Signals and what they mean together

| Atlas signal | DB-internal signal | Combined read |
|---|---|---|
| CPU p95 > 80% | `scanned_per_sec_approx` high relative to `opcounters.query` | Root cause is likely missing/poor indexes, not undersized compute. Recommend an index review (Atlas Performance Advisor or `explain()`) before a tier bump. |
| Free memory low / high cache churn | `wt_cache_pct_used` near 100% **and** `working_set` total (sum of `data_size_bytes` + `index_size_bytes` for hot collections) exceeds `wt_cache_bytes_max` | Working set genuinely doesn't fit in RAM at the current tier — this is a real memory-driven scale-up case, not noise. Confidence: high. |
| Free memory low, but... | `wt_cache_pct_used` is moderate and working set fits well under `wt_cache_bytes_max` | The OS-level memory pressure isn't coming from WiredTiger — could be another process, the OS page cache doing its normal thing, or a short spike. Lower confidence on a memory-driven scale-up; don't recommend one from this signal alone. |
| Connections p95 near limit | `connections_pct_used` from serverStatus agrees | Corroborated — real connection pressure, likely a connection-pooling problem in the app as much as a tier problem. Worth mentioning both fixes. |
| Connections p95 near limit | `connections_pct_used` is low | Disagreement — the Atlas-side number may reflect a different sampling window or a spike that's since resolved. Note the discrepancy in the report rather than picking one silently. |
| Disk IOPS/utilization high | `wt_pages_read_into_cache_per_sec` high | Cache misses are driving disk reads — again points at working-set-vs-cache-size, reinforcing (or explaining) the IOPS signal rather than it being pure write-volume. |

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
  inspected (for `dbStats`/`collStats`). Don't ask for more than that.
- On a sharded cluster, connect to `mongos` for cluster-wide dbStats, but also consider running
  `serverStatus` against individual shard primaries if a specific shard is suspected — a
  mongos-level view averages away a single hot shard the same way cluster-wide Atlas metrics can.
- This complements `rightsizing.py`; it doesn't replace it. Hardware metrics still matter for
  raw CPU/IOPS ceiling questions that db-internal stats don't see directly.
