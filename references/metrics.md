# Metrics pulled from the Atlas Admin API

Two endpoints, because Atlas splits host-level and disk-level measurements (search nodes use
the host-level one; see "Search node metrics" below):

- **Host-level**: `GET /api/atlas/v2/groups/{groupId}/processes/{processId}/measurements`
- **Disk-level**: `GET /api/atlas/v2/groups/{groupId}/processes/{processId}/disks/{partitionName}/measurements`
  (querying a disk metric against the host-level endpoint 404s with `INVALID_METRIC_NAME` —
  they're genuinely separate resources, not just a documentation quirk.)

Query params used on both: `granularity=PT1H&period=P{N}D&m=<metric>` (repeat `m` for multiple
metrics in one call — the API accepts several `m` params per request, which keeps call count
down). `--hours` runs use `granularity=PT1M` (or `PT1H` beyond Atlas's ~38h PT1M retention
window) instead — see `rightsizing.py`'s `main()`.

> Measurement names are an Atlas API enum that occasionally changes. Before relying on this list
> for a real run, cross-check against the current `MeasurementView` `name` enum in the Atlas
> Admin API reference — the script fails loudly (400/404 from the API) rather than silently if a
> name is wrong, so this is low-risk to verify empirically too. Several names below were
> corrected from an earlier, unverified version of this list — see `references/thresholds.md`
> for what changed and why.

## Host-level metrics (`HOST_METRICS` in `rightsizing.py`)

| Metric | Why it matters for rightsizing |
|---|---|
| `SYSTEM_NORMALIZED_CPU_USER` | CPU usage normalized against the instance's tier — the right signal for cross-tier scale-up/down comparisons (raw `SYSTEM_CPU_USER` isn't comparable across tiers). |
| `SYSTEM_NORMALIZED_CPU_KERNEL` | Kernel-time CPU; sustained high values alongside high IOPS often means the box is disk/interrupt-bound, not just query-load-bound. |
| `SYSTEM_MEMORY_USED` / `FREE` / `CACHED` / `BUFFERS` / `AVAILABLE` | Working-set pressure. Low free memory alone is a weaker signal than it looks (see `EXTRA_INFO_PAGE_FAULTS` below) — it's often just Linux using spare RAM for filesystem page cache. |
| `CONNECTIONS` | Compare against the tier's max-connections limit (`TIER_MAX_CONNECTIONS`); consistently near the ceiling is its own scale-up trigger regardless of CPU/memory. |
| `TICKETS_AVAILABLE_READS` / `TICKETS_AVAILABLE_WRITE` | WiredTiger concurrency tickets — pulled for **context only**, not a trigger (see `references/thresholds.md` "Concurrency queuing" for why the old ticket-count trigger was removed). Note the Atlas API naming quirk: `READS` is plural, `WRITE` is singular. |
| `GLOBAL_LOCK_CURRENT_QUEUE_READERS` / `WRITERS` | The real concurrency-pressure trigger: operations actually queued waiting for a slot right now, not just a low ticket count. |
| `OP_EXECUTION_TIME_READS` / `WRITES` | Context only — corroborates queue-depth findings with actual latency impact. |
| `EXTRA_INFO_PAGE_FAULTS` | Rate (per second). A more direct signal of an undersized WT cache than free memory — a rising fault rate means MongoDB is actually going to disk for pages that should be cached. |
| `CACHE_BYTES_READ_INTO` | Rate (bytes/sec). Context only — corroborates that page faults are cache-driven. |
| `CACHE_FILL_RATIO` / `DIRTY_FILL_RATIO` | WT eviction pressure, already scaled 0–100 (`units: PERCENT` — do not multiply by 100). See "WiredTiger eviction thresholds" in `references/thresholds.md` for the `eviction_target`/`eviction_dirty_target` triggers these compare against. |
| `SYSTEM_NORMALIZED_CPU_IOWAIT` | Context only — corroborates disk-latency findings with actual CPU stall time. |

Derived per data point (before percentiles, since a sum of percentiles isn't the percentile of
the sum): `SYSTEM_NORMALIZED_CPU_TOTAL` (user + kernel) and `SYSTEM_MEMORY_AVAILABLE_PERCENT`
(available / (used + free + cached + buffers)). Those four sum to physical RAM (verified on live
M40/M60 nodes: 15.6 / 62.8 GiB). `SYSTEM_MEMORY_FREE` excludes the page cache, so a busy mongod
shows ~3% free while ~44% is actually available — `SYSTEM_MEMORY_AVAILABLE` is the pressure signal.

## Disk-level metrics (`DISK_METRICS` in `rightsizing.py`)

| Metric | Why it matters for rightsizing |
|---|---|
| `DISK_PARTITION_IOPS_READ` / `DISK_PARTITION_IOPS_WRITE` | Compare against the cluster's *provisioned* IOPS (from the cluster config call) to compute headroom. |
| `DISK_PARTITION_SPACE_PERCENT_USED` | Disk **capacity** consumed (`used/(used+free)*100`), NOT I/O saturation — an earlier version of this list called the equivalent field "disk utilization" and treated it as an I/O-busy signal; `DISK_PARTITION_UTILIZATION` doesn't actually exist as a measurement name (404s). Still a legitimate trigger (running out of space is a real problem), just correctly named now. |
| `DISK_PARTITION_SPACE_USED` / `DISK_PARTITION_SPACE_FREE` | Raw values backing the percentage above — used to sanity-check it and for absolute-size context in the report. |
| `DISK_PARTITION_LATENCY_READ` / `WRITE` | The real "is the disk keeping up" signal (ms per op) — see `references/thresholds.md` "Disk latency / throughput / iowait." No ceiling needed to be meaningful, unlike IOPS. |
| `DISK_PARTITION_THROUGHPUT_READ` / `WRITE` | Context only — Atlas exposes no "provisioned throughput" ceiling to compare against, unlike `diskIOPS`. |

Derived per data point, per partition: `DISK_PARTITION_IOPS_TOTAL` (read + write), compared
against provisioned IOPS since reads and writes share that budget.

For sharded clusters, pull the same metrics per shard's processes and evaluate each shard
independently — one hot shard shouldn't get masked by averaging across a well-balanced cluster.
`rightsizing.py` does this by grouping processes on `replicaSetName`; see its
`group_processes_by_replica_set()` docstring for caveats (not verified against a live sharded
cluster).

## Search node metrics (`SEARCH_NODE_METRICS` in `rightsizing.py`)

Dedicated search nodes (mongot) aren't in `/processes`, but given a hostname from a mongot/search
event, the host-level endpoint serves their measurements:
`GET /api/atlas/v2/groups/{groupId}/processes/{host}:{port}/measurements` (verified live against
S30 and S100 search nodes, port 28000). There's no disk sub-resource for them. See "Search nodes"
in `references/thresholds.md` for how nodes are found and the rules applied.

| Metric | Why it matters for rightsizing |
|---|---|
| `SYSTEM_NORMALIZED_CPU_USER` / `KERNEL` | Same as on mongod: summed into `SYSTEM_NORMALIZED_CPU_TOTAL` for the CPU trigger and the node-count projection. |
| `SYSTEM_NORMALIZED_CPU_IOWAIT` | Context only. |
| `SYSTEM_MEMORY_USED` / `FREE` / `CACHED` / `BUFFERS` / `AVAILABLE` | Kilobytes. Same available-memory trigger as mongod. Their sum is total RAM, the denominator for index size / RAM. |
| `FTS_DISK_USAGE` | Bytes. On-disk size of the search indexes on this node. mongot memory-maps the index and serves queries through the OS page cache, so the index outgrowing RAM is the search equivalent of a working set outgrowing the WT cache. |
| `FTS_PROCESS_RESIDENT_MEMORY` | Bytes. Context only. It tracks the index size (seen live: ~188 GB resident for a ~185 GB index), most likely because mapped index pages count as resident (not verified), so it isn't a pressure signal on its own. |

Derived per data point: `SYSTEM_NORMALIZED_CPU_TOTAL`, `SYSTEM_MEMORY_AVAILABLE_PERCENT` (as on
mongod), and `SEARCH_INDEX_RAM_PERCENT` (`FTS_DISK_USAGE` / (total RAM KB × 1024)). The `FTS_*`
and `SYSTEM_*` series are sampled on different clocks even within one response (seen live: FTS
points at :39 past each hour, 168 of them; SYSTEM points at :17, 167 of them). So
`SEARCH_INDEX_RAM_PERCENT` pairs each index-size point with the nearest-in-time RAM point, and the
script exits if none is within one granularity step. Pairing by position would mismatch readings
taken up to ~40 minutes apart, or crash when the counts differ.
