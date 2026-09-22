# Rightsizing decision rules

These are defaults tuned for "don't cry wolf, but don't miss a real problem." Adjust per user
risk tolerance — a user optimizing hard for cost will want lower scale-down bars; a user who
says latency/incidents matter most wants lower scale-up bars and should probably keep more
headroom than these defaults leave.

## Scale UP triggers (any one is sufficient to recommend, list all that fired)

| Signal | Threshold | Window |
|---|---|---|
| Normalized CPU (user+kernel) | p95 > 80% | sustained across ≥ 3 of the last 7 days |
| Memory | free memory p95 < 10% of total, or rising eviction/cache-churn trend | last 7 days |
| Disk IOPS | p95 > 90% of provisioned IOPS | sustained across ≥ 3 of the last 7 days |
| Disk space used | p95 > 90% of capacity (NOT I/O saturation — see below) | sustained across ≥ 3 of the last 7 days |
| Disk latency | read or write p95 > 15ms (heuristic, see below) | last 7 days |
| Connections | p95 > 80% of tier's max connections | last 7 days |
| Concurrency queuing | read or write queue depth p95 > 0 ops (see below — NOT ticket count) | last 7 days |
| Page faults | p95 > 1.0/sec (heuristic, see below) | last 7 days |
| Cache fill ratio | p95 > 80% (WT `eviction_target`, see below) | last 7 days |
| Dirty fill ratio | p95 > 5% (WT `eviction_dirty_target`, see below) | last 7 days |

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

The script triggers on the `_target` values (80% / 5%), not the harder `_trigger` values (95% /
20%). This is a deliberate choice to flag sustained *background* eviction pressure before it
escalates to the fully pressured, application-thread-stalling state — it will fire earlier and
more often than a trigger-based threshold would. If false positives become a problem for a
particular workload, consider moving these to the `_trigger` values instead (95% / 20%) for a
more conservative (later, less sensitive) signal.

`CACHE_FILL_RATIO` and `DIRTY_FILL_RATIO` from the Atlas Admin API report `units: PERCENT` as
already-scaled 0–100 values (e.g. a reading of `78.3` means 78.3%) — **do not** multiply by 100.

## Scale DOWN candidate (requires ALL of the following — this should be a conservative call)

- Normalized CPU p95 < 20% for the entire window, not just average
- Memory: free memory consistently > 40% of total
- Disk IOPS and utilization both well under 50% of their ceilings
- Connections well under the tier's limit
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

**This is different from "insufficient data."** If a cluster/shard returns zero metrics for the
whole requested window (too new — check its creation time — paused, or unreachable), the script
reports `insufficient_data` / confidence `low`, not `no_change` / high confidence. Confirmed as a
real failure mode: a cluster created ~30 minutes before a 7-day audit ran against it returned a
completely empty metric table, and without this distinction the script reported "no thresholds
crossed, confidence: high" — indistinguishable from a genuinely healthy, well-observed cluster.
Never treat an empty metrics table as "no change" yourself either, even if summarizing verbally.

## Confidence levels

- **High**: ≥ 7 days of clean data, signal consistent across the whole window, single clear
  driver.
- **Medium**: shorter window (3–6 days), or signal present but not on every day, or multiple
  competing signals.
- **Low**: < 3 days of data, or metrics near the threshold boundary, or gaps in the data (node
  restarts, etc.). Say so and suggest re-running with a longer window before acting.

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
