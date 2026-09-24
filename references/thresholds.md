# Rightsizing decision rules

These are defaults tuned for "don't cry wolf, but don't miss a real problem." Adjust per user
risk tolerance — a user optimizing hard for cost will want lower scale-down bars; a user who
says latency/incidents matter most wants lower scale-up bars and should probably keep more
headroom than these defaults leave.

## Scale UP triggers (any one is sufficient to recommend, list all that fired)

| Signal | Threshold | Window |
|---|---|---|
| Normalized CPU (user+kernel) | p95 > 80% | sustained across ≥ 3 of the last 7 days |
| Memory | available memory p5 < 10% of total RAM (`SYSTEM_MEMORY_AVAILABLE` / (used+free+cached+buffers) per data point; low-is-bad, so the bottom tail) | last 7 days |
| Disk IOPS | read+write p95 > 90% of provisioned IOPS (one shared budget) | sustained across ≥ 3 of the last 7 days |
| Disk space used | p95 > 90% of capacity (NOT I/O saturation — see below) | sustained across ≥ 3 of the last 7 days |
| Disk latency | read or write p95 > 15ms (heuristic, see below) | last 7 days |
| Connections | p95 > 80% of tier's max connections | last 7 days |
| Concurrency queuing | read or write queue depth p95 > 0 ops (see below — NOT ticket count) | last 7 days |
| Page faults | p95 > 1.0/sec (heuristic, see below) | last 7 days |
| Cache fill ratio | p95 > 95% (WT `eviction_trigger`, see below) | last 7 days |
| Dirty fill ratio | p95 > 20% (WT `eviction_dirty_trigger`, see below) | last 7 days |

If multiple signals fire together (e.g. CPU + IOPS), say so explicitly — it changes the fix
(compute tier bump vs. IOPS bump vs. both) and a single-signal trigger deserves less confidence
than a multi-signal one.

### Disk latency / throughput / iowait (and the "disk utilization" mislabeling)

**Correction:** earlier versions of this script and this doc called this trigger "disk
utilization" and treated it as an I/O-saturation signal (the classic `iostat %util` meaning —
"what fraction of the time is the disk busy"). It isn't one. The underlying metric,
`DISK_PARTITION_SPACE_PERCENT_USED`, was verified against `DISK_PARTITION_SPACE_USED` /
`DISK_PARTITION_SPACE_FREE` at matching timestamps and matches `used/(used+free)*100` exactly —
it's disk **capacity** consumed, not I/O saturation. Still a legitimate trigger (running out of
disk space is a real problem), just correctly named "disk space used" now, both in code and here.

The script had no real I/O-saturation/throughput signal at all until this was added. The fix:

- **`DISK_PARTITION_LATENCY_READ`/`WRITE`** (ms per operation) is the trigger. Latency is the
  right choice over throughput because it needs no ceiling to be meaningful — rising per-op
  latency directly means queries are getting slower, regardless of the disk's absolute capacity.
  The `p95 > 15ms` cutoff is a heuristic, **not derived from MongoDB/Atlas documentation** (same
  treatment as the page-fault threshold) — tune it against your own workload's baseline.
- **`DISK_PARTITION_THROUGHPUT_READ`/`WRITE`** (bytes/sec) are pulled for context only. Unlike
  `diskIOPS`, Atlas's cluster config doesn't expose a "provisioned throughput" ceiling to compare
  against, so there's no principled way to turn a raw bytes/sec number into a trigger threshold
  that means the same thing across different tiers and disk types.
- **`SYSTEM_NORMALIZED_CPU_IOWAIT`** is pulled for context too — rising iowait alongside rising
  latency corroborates that the CPU is actually stalling on disk I/O (a more direct
  application-impact signal than latency alone), and is included in the trigger's reason text
  when elevated (p95 > 5%) at the same time latency fires.

### Connections (correction: was documented but never implemented)

This row existed in this doc from early in the script's history, but `CONNECTIONS` was only ever
pulled into the report table — there was no actual threshold logic behind it. Now implemented
against `TIER_MAX_CONNECTIONS` in `rightsizing.py`. **Verification status: only the M10 value
(1500) has been independently confirmed**, by reading `db_diagnostics.py`'s live
`serverStatus().connections` against a real M10 cluster (`current` + `available` summed to
exactly 1500). The other tiers are MongoDB's commonly published Atlas limits, not re-verified in
this project — confirm against a live cluster or Atlas's own documentation before trusting them,
the same caveat as the code comment on `TIER_MAX_CONNECTIONS`.

### Concurrency queuing (not WiredTiger ticket count)

An earlier version of this script triggered on `TICKETS_AVAILABLE_READS`/`TICKETS_AVAILABLE_WRITE`
being low (p50 < 5). **That trigger was removed — it produces false positives on MongoDB 7.0+.**

Before 7.0, WiredTiger used a static concurrency ticket pool (128 read / 128 write by default,
fixed unless manually configured), so a low "tickets available" reading reliably meant the pool
was nearly exhausted. MongoDB 7.0 introduced dynamic execution control: the ticket pool is now
resized automatically based on observed throughput. A low "available" count no longer reliably
means exhaustion — it can just as easily mean the algorithm sized the pool small because the
workload is genuinely light, and there is no Atlas-exposed metric for the pool's *current total*
size to compute a real utilization ratio against.

This was confirmed empirically, not just theoretically: on this skill's own MongoDB 8.0 test
cluster, `TICKETS_AVAILABLE_READS` held at p50=4 for a full week — which the old rule flagged as
SCALE UP every single run — while `GLOBAL_LOCK_CURRENT_QUEUE_READERS` stayed at p95=0, max=0 for
the entire window. Nothing was ever actually waiting for a ticket.

**The fix: trigger on queue depth instead.** `GLOBAL_LOCK_CURRENT_QUEUE_READERS`/`WRITERS` count
operations actually queued waiting for a concurrency slot right now — the real symptom, not an
internal pool-sizing artifact — and mean the same thing regardless of MongoDB version or whether
the ticket pool is static or dynamic. The `p95 > 0` cutoff (rather than `max > 0`) is deliberate:
a single momentary blip to 1 queued operation is normal noise, not a signal; requiring it in more
than 5% of samples filters that out while still catching recurring contention. `OP_EXECUTION_TIME_
READS`/`WRITES` are pulled as corroborating context (rising execution time alongside queuing
strengthens the case that it's actually impacting the workload), and `TICKETS_AVAILABLE_READS`/
`WRITE` are still pulled for context too — just no longer as the trigger.

### WiredTiger cache pressure (page faults)

`EXTRA_INFO_PAGE_FAULTS` (rate, per second) is a more direct signal of an undersized WT cache than
`SYSTEM_MEMORY_FREE`: low free host memory is often just Linux using spare RAM for filesystem page
cache and is not itself a problem, whereas a rising page-fault rate means MongoDB is actually going
to disk for pages that should be in the WT cache. The `p95 > 1.0/sec` cutoff is **not derived from
MongoDB/Atlas documentation** — it's a conservative, sensitive-by-design heuristic (Atlas storage is
SSD-backed, so faulting is cheap per-occurrence, but a sustained rate above roughly-zero is still
worth surfacing). Treat a lone page-fault trigger as weaker evidence than CPU/IOPS/disk-utilization
triggers, and check whether `CACHE_BYTES_READ_INTO` is also elevated (pulled for context, not itself
a trigger) before recommending a RAM-focused tier bump. Tune this threshold against your own
workload's baseline once you have a few reports to compare.

### WiredTiger eviction thresholds (cache fill / dirty fill ratio)

WiredTiger has four built-in eviction thresholds, as percentages of the *configured* cache size
(not host RAM):

| Parameter | Default | Behavior |
|---|---|---|
| `eviction_target` | 80% | Background eviction threads start working to bring overall cache usage back down |
| `eviction_trigger` | 95% | **Pressured state**: application threads are recruited to evict pages themselves, stalling reads/writes |
| `eviction_dirty_target` | 5% | Background eviction starts working on dirty (not-yet-checkpointed) pages |
| `eviction_dirty_trigger` | 20% | **Pressured state**: application threads are recruited to evict dirty pages specifically — more expensive than clean-page eviction since it requires a disk write |

The script triggers on the `_trigger` values (95% / 20%), where application threads are recruited
to evict and operations stall. It used to trigger on the `_target` values (80% / 5%), but WiredTiger
holds a full cache at `eviction_target` by design: on a live project, every node of every cluster
whose data exceeds its cache showed a cache-fill p95 of 80.0x and fired, whether or not anything
was stalling.

`CACHE_FILL_RATIO` and `DIRTY_FILL_RATIO` from the Atlas Admin API report `units: PERCENT` as
already-scaled 0–100 values (e.g. a reading of `78.3` means 78.3%) — **do not** multiply by 100.

## Scale DOWN candidate (requires ALL of the following — this should be a conservative call)

- Normalized CPU p95 < 20% for the entire window, not just average
- Memory: available memory p5 > 40% of total RAM
- Disk space used p95 < 50%, and read+write IOPS p95 < 50% of provisioned IOPS — if the cluster
  config has no provisioned IOPS value, scale-down isn't recommended
- Connections p95 < 30% of the tier's limit — if the tier isn't in `TIER_MAX_CONNECTIONS`,
  scale-down isn't recommended
- No gaps in the core metrics (every one ≥ 90% non-null data points)
- At least 7 full days of data (30 preferred) with no gaps, and the window should be checked
  against the user for known low-traffic periods (don't recommend downsizing off of a holiday
  week's data)

Report scale-down candidates with an explicit confidence caveat and ask whether the lookback
window captures peak traffic (month-end, seasonal, marketing campaigns) before treating it as
a strong recommendation.

## No change

None of the above triggers, OR signals are mixed/borderline (e.g. p95 CPU at 55%, comfortably
mid-range) — say so plainly rather than forcing a recommendation. "Currently well-matched" is a
valid and useful output.

**This is different from "insufficient data."** If any core metric (`CORE_METRICS` in
`rightsizing.py`: normalized CPU user/kernel, memory used/free, connections, disk IOPS read/write,
disk space used) has no data points in the requested window (too new — check its creation time —
paused, or unreachable), the script reports `insufficient_data` / confidence `low`, not
`no_change` / high confidence. Every threshold check is guarded on its metric being present, so
without this a missing metric would silently skip its checks. Confirmed as a real failure mode: a
cluster created ~30 minutes before a 7-day audit ran against it returned a completely empty metric
table, and the script reported "no thresholds crossed, confidence: high". Never treat an empty
metrics table as "no change" yourself either, even if summarizing verbally.

API errors (including a 429 that persists after retries) and requested measurement names missing
from a response stop the script rather than dropping that metric.

## Confidence levels

- **High**: ≥ 7 days of clean data and none of the Low conditions; for a scale-up, at least two
  signals fired (a single-signal scale-up is Medium).
- **Medium**: 3–6 day window, a single-signal scale-up, or a check that couldn't run for lack of
  a ceiling (no provisioned `diskIOPS` in the cluster config, or a tier missing from
  `TIER_MAX_CONNECTIONS`) — listed under "Confidence notes".
- **Low**: < 3 days of data, or gaps in the data — any core metric with less than 90% non-null
  data points (`MIN_COVERAGE`; node restarts, a pause, a cluster newer than the window) — or any
  threshold check with its value within 10% of the cutoff on either side (`NEAR_THRESHOLD_BAND`).
  The report lists which of these applied under "Confidence notes". Say so and suggest re-running
  with a longer window before acting.

## Search nodes

Dedicated search nodes (mongot, S-tiers such as `S30_LOWCPU_NVME`) are evaluated separately from
the cluster's mongod nodes, as `component: "search"` results named
`<cluster> — search nodes <group> (<count>)`. `<group>` is the `shard-NN` / `config-NN` label in
the search hostnames, which matches the mongod shard the nodes serve. Seen live: a replica set's
search nodes are `shard-00`, and a config shard's (a config server that also holds data) are
`config-00`. As with mongod,
each node is evaluated on its own and the group's verdict is the worst node's. `merge_verdict.py`
leaves these results out, because `db_diagnostics.py` only sees mongod/mongos.

### Finding the nodes

Search nodes aren't in `/processes` and have no Admin API listing of their own. The script:

1. Reads `GET /clusters/{name}/search/deployment`. `{}` means no search nodes.
2. Collects hostnames from host-level mongot/search events (`SEARCH_HOST_EVENTS`) over the last
   90 days (`SEARCH_DISCOVERY_DAYS`). Atlas restarts mongot at maintenance, so every current node
   has had an event in that window.
3. Fetches `GET /processes/{host}:{port}/measurements` per host. `HOST_NOT_FOUND` means the node
   was replaced since its event, and it is skipped.
4. Exits if the number of live nodes found doesn't equal the deployment spec: the sum of
   `effectiveSpecs[].nodeCount`, times the number of shards when the specs have no `shardId`
   (verified live: 4 shards × `nodeCount` 9 = 36 hosts).

### Scale UP triggers (any one is sufficient)

| Signal | Threshold | Window |
|---|---|---|
| Normalized CPU (user+kernel) | p95 > 80% | requested window |
| Memory | available memory p5 < 10% of total RAM (same formula as mongod) | requested window |
| Search index size / RAM | `FTS_DISK_USAGE` as a share of total RAM, p95 > 90% (heuristic, see below) | requested window |
| mongot OOM crash | any `HOST_MONGOT_CRASHING_OOM` event | requested window |
| Search process throttled | any `HOST_SEARCH_PROCESS_THROTTLING` event | requested window |
| Disk nearly full / full | any `HOST_MONGOT_APPROACHING_STOP_REPLICATION` or `HOST_MONGOT_STOP_REPLICATION` event | requested window |

**Index size vs. RAM.** mongot serves queries from its index through the OS page cache: the index
is memory-mapped, which is why available memory stays high even on a busy node. Once the index
outgrows RAM, queries start going to disk. The 90% cutoff is a heuristic, **not derived from
MongoDB/Atlas documentation**. It is computed per data point, before percentiles are taken.
`FTS_*` series come from mongot and `SYSTEM_*` series from the host, and they are sampled on
different clocks. In one live response the FTS points were at :39 past each hour (168 points) and
the SYSTEM points at :17 (167 points). So each index-size point is paired with the nearest-in-time
total-RAM point, and the script exits if none is within one granularity step.

**Events stand in for the disk check.** Atlas exposes no disk capacity or IOPS for search nodes.
The replication-stop events are the only disk-pressure signal.

### Scale DOWN candidate (requires ALL of the following)

- Normalized CPU p95 < 20%
- Search index size / RAM p95 < 40%, so the index would still fit under 80% in a tier with half
  the RAM
- No pressure events in the window
- No gaps in the core metrics (CPU, available memory, index size / RAM; each ≥ 90% non-null)
- At least 7 days of data

### Node count

Node count is separate from tier: it sets query throughput and redundancy. When a group qualifies,
the script suggests `reduce_node_count_candidate` (or adds the suggestion to a
`scale_down_candidate`). A group qualifies when all of these hold:

- its verdict is `no_change` or `scale_down_candidate`
- confidence is medium or high
- the window is ≥ 7 days
- every node's CPU p95 is under 20%

The target is `max(2, ceil(max node CPU p95 × node count / 50))`, the fewest nodes that keep the
projected per-node CPU p95 under 50% (`SEARCH_TARGET_CPU_PCT`). Atlas's minimum is 2
(`SEARCH_MIN_NODES`). The projection assumes load spreads evenly across nodes. That is a
heuristic, and it hasn't been validated against an actual node reduction.

- **No `shardId` in the spec:** `nodeCount` is one setting for every shard. A reduction is
  suggested only if every group qualifies, and the busiest group sets the target.
- **Several `effectiveSpecs` (multi-region):** the check is skipped. Hostnames don't show which
  region a node is in.
- Always tell the user that cutting nodes also cuts redundancy for search queries. Check
  availability needs before acting on it.

Confidence follows the same rules as mongod (see below). If search node autoscaling is configured,
the report notes it, and a scale verdict is about the autoscaling bounds.

## M-tier reference (vCPU / RAM) — for reasoning about headroom between tiers

Use this to describe *how much* headroom a scale-up/down would add, not just that one is
recommended. Confirm exact current specs against the Atlas UI or `GET /clusters/{name}` response
before quoting numbers to a user, since Atlas periodically revises tier specs.

| Tier | vCPU (approx) | RAM (approx) |
|---|---|---|
| M10 | 2 | 2 GB |
| M20 | 2 | 4 GB |
| M30 | 2 | 8 GB |
| M40 | 4 | 16 GB |
| M50 | 8 | 32 GB |
| M60 | 16 | 64 GB |
| M80 | 32 | 128 GB |
| M140/M200/M300 | 64+ | 192 GB+ |

Dedicated tiers (M10+) support independently provisioned IOPS on most cloud providers — a disk
bottleneck doesn't always require a compute tier change; check whether bumping IOPS alone
resolves it before recommending a full tier jump.
