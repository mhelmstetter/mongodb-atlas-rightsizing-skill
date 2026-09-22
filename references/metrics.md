# Metrics pulled from the Atlas Admin API

Endpoint: `GET /api/atlas/v2/groups/{groupId}/processes/{processId}/measurements`
Query params used: `granularity=PT1H&period=P{N}D&m=<metric>` (repeat `m` for multiple metrics
in one call — the API accepts several `m` params per request, which keeps call count down).

> Measurement names are an Atlas API enum that occasionally changes. Before relying on this list
> for a real run, cross-check against the current `MeasurementView` `name` enum in the Atlas
> Admin API reference — the script fails loudly (400 from the API) rather than silently if a name
> is wrong, so this is low-risk to verify empirically too.

| Metric | Why it matters for rightsizing |
|---|---|
| `SYSTEM_NORMALIZED_CPU_USER` | CPU usage normalized against the instance's tier — the right signal for cross-tier scale-up/down comparisons (raw `SYSTEM_CPU_USER` isn't comparable across tiers). |
| `SYSTEM_NORMALIZED_CPU_KERNEL` | Kernel-time CPU; sustained high values alongside high IOPS often means the box is disk/interrupt-bound, not just query-load-bound. |
| `SYSTEM_MEMORY_USED` / `SYSTEM_MEMORY_FREE` | Working-set pressure. Low free memory + high page faults is a stronger upgrade signal than CPU alone for MongoDB, since WiredTiger cache eviction stalls hurt latency before CPU maxes out. |
| `CACHE_BYTES_READ_INTO` / `CACHE_BYTES_WRITTEN_FROM` | WiredTiger cache churn. High values relative to cache size suggest working set doesn't fit in RAM — a memory/tier problem, not just CPU. |
| `DISK_PARTITION_IOPS_READ` / `DISK_PARTITION_IOPS_WRITE` | Compare against the cluster's *provisioned* IOPS (from the cluster config call) to compute headroom. |
| `DISK_PARTITION_UTILIZATION` | Percent-busy on the data volume; a more direct "is disk the bottleneck" signal than raw IOPS. |
| `DISK_PARTITION_SPACE_USED` / `DISK_PARTITION_SPACE_FREE` | Storage growth — a separate axis from compute/IOPS rightsizing, but worth flagging if free space is trending toward exhaustion. |
| `CONNECTIONS` | Compare against the tier's max-connections limit; consistently near the ceiling is its own scale-up trigger regardless of CPU/memory. |
| `TICKETS_AVAILABLE_READS` / `TICKETS_AVAILABLE_WRITES` | WiredTiger concurrency tickets. Values frequently near zero indicate contention the other metrics might not surface directly. |
| `OPCOUNTER_QUERY` / `OPCOUNTER_INSERT` / `OPCOUNTER_UPDATE` / `OPCOUNTER_DELETE` | Operational context for the report (workload shape), not a direct threshold trigger. |

For sharded clusters, pull the same metrics per shard's processes and evaluate each shard
independently — one hot shard shouldn't get masked by averaging across a well-balanced cluster.
