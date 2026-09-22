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
| Disk utilization | p95 > 90% | sustained across ≥ 3 of the last 7 days |
| Connections | p95 > 80% of tier's max connections | last 7 days |
| WiredTiger tickets | available reads or writes frequently at/near 0 | last 7 days |

If multiple signals fire together (e.g. CPU + IOPS), say so explicitly — it changes the fix
(compute tier bump vs. IOPS bump vs. both) and a single-signal trigger deserves less confidence
than a multi-signal one.

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
