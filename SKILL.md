---
name: mongodb-rightsizing
description: Generate MongoDB Atlas cluster rightsizing recommendations by pulling hardware metrics (CPU, memory, disk IOPS, disk utilization, connections) from the Atlas Admin API and comparing them against tier-appropriate thresholds. Use this skill whenever the user asks about Atlas cluster sizing, whether a cluster is over- or under-provisioned, cost optimization for Atlas clusters, scaling recommendations, "is my cluster too big/small", capacity planning, or wants a report on cluster hardware utilization. Also trigger for requests to audit multiple clusters/projects for sizing issues.
---

# MongoDB Atlas Rightsizing

Produces a data-backed recommendation (scale up / scale down / no change) for one or more Atlas
clusters, using real hardware metrics from the Atlas Admin API rather than guesswork.

## Prerequisites

The user needs Atlas API credentials with at least **Project Read Only** access:
- An Atlas **Service Account** (`client_id` / `client_secret`, OAuth2) — preferred, or
- Legacy **Organization/Project API key** (`public_key` / `private_key`, HTTP Digest auth)

They also need the **Project (Group) ID** — found in Atlas UI under Project Settings, or via
`GET /api/atlas/v2/groups`.

If the user doesn't have these yet, point them to:
https://www.mongodb.com/docs/atlas/configure-api-access/

## Workflow

1. **Collect inputs** from the user (or a config file — see `scripts/config.example.json`):
   - Project ID (`groupId`)
   - Cluster name(s) — or "all" to audit every cluster in the project
   - Credentials (never ask the user to paste secrets into chat if avoidable — prefer env vars:
     `ATLAS_CLIENT_ID`/`ATLAS_CLIENT_SECRET` or `ATLAS_PUBLIC_KEY`/`ATLAS_PRIVATE_KEY`)
   - Lookback window (default: 7 days — long enough to smooth daily cycles, short enough to stay
     current). Offer 30 days if the user cares about monthly peaks (e.g. batch jobs, reporting).

2. **Run the script**:
   ```bash
   python scripts/rightsizing.py \
     --group-id <groupId> \
     --cluster <clusterName>   # omit to audit all clusters in the project \
     --days 7 \
     --out-dir ./rightsizing-report
   ```
   This writes `report.md` (human-readable) and `report.json` (structured) to `--out-dir`.
   See `scripts/rightsizing.py --help` for auth flag details.

3. **What the script does** (for each target cluster):
   - Fetches cluster config: `GET /groups/{groupId}/clusters/{clusterName}` — current tier,
     disk size, provisioned IOPS, whether autoscaling is on.
   - Fetches the process list for the cluster: `GET /groups/{groupId}/processes` (filtered by
     cluster name) to get each node's `processId` (host:port) and type (primary/secondary/analytics).
   - Fetches measurements per process: `GET /groups/{groupId}/processes/{processId}/measurements`
     with `granularity=PT1H` and `period=P{days}D`, for the metric set in
     `references/metrics.md`.
   - Computes p50/p95/max for each metric across the window, per node and cluster-wide.
   - Applies the decision rules in `references/thresholds.md` to classify each cluster as
     **scale up**, **scale down**, **change disk/IOPS only**, or **no change**, with a
     confidence level based on how many days of data were available and how consistently the
     signal held.

4. **Present the results**: show `report.md` inline (it's short and this is usually what the
   user actually wants to read), and mention `report.json` is available if they're feeding this
   into another system or want to diff runs over time.

5. **Caveats to state explicitly in the report** (the script includes these, but repeat them if
   summarizing verbally):
   - This is a recommendation, not an autoscaling action — nothing is changed in Atlas.
   - Metrics reflect the lookback window only; call out if it doesn't cover a known peak period
     (month-end batch, Black Friday, etc.) and suggest widening `--days` or targeting a specific
     historical window if the API's period supports it.
   - Sharded clusters: run per-shard, since rightsizing decisions differ per shard. The script
     handles this automatically when it detects a sharded topology, but say so in the summary.
   - Free/shared tier (M0/M2/M5) clusters don't expose full hardware measurements — the script
     will note this and skip metric-based analysis for them.

## Optional: database-internal diagnostics (when the user has DB credentials)

The Atlas Admin API only sees OS/hardware-level metrics. It can tell you the box is under
pressure, but not *why* — a CPU spike from a missing index looks identical, from Atlas's view,
to one from genuine undersizing. If the user has (or can get) a MongoDB user with `clusterMonitor`
and read access — not just Atlas API credentials — run `scripts/db_diagnostics.py` alongside
`rightsizing.py` and read `references/db-diagnostics.md` before writing the combined
recommendation. This is what lets the report say "add an index" instead of "buy a bigger cluster"
when that's the actual fix. Ask the user whether they have this level of access before assuming
Atlas-only; don't ask them to create new database users just for this unless they want to.

```bash
python scripts/db_diagnostics.py --uri "<connection string>" --sample-interval 60 --out-dir ./rightsizing-report
```

Note this takes at least `--sample-interval` seconds to run (it samples `serverStatus()` twice
to compute rates from cumulative counters) — tell the user before starting it.

## Files

- `scripts/rightsizing.py` — pulls Atlas hardware metrics. Self-contained, only depends on `requests`.
- `scripts/db_diagnostics.py` — optional second data source: `serverStatus()`/`dbStats()`/
  `collStats()` via a direct MongoDB connection. Depends on `pymongo`.
- `scripts/config.example.json` — example config file (alternative to CLI flags/env vars, useful
  for auditing many clusters across projects on a schedule).
- `references/metrics.md` — the exact Atlas measurement names pulled and why each matters.
- `references/thresholds.md` — the decision rules (the actual rightsizing logic) and the M-tier
  reference table (vCPU/RAM per tier) used to reason about headroom.
- `references/db-diagnostics.md` — how to combine the two data sources, including what to do
  when they disagree.

Read `references/thresholds.md` before hand-tuning any thresholds for a user's risk tolerance
(e.g. a user who says "we never want to be CPU-bound" wants a lower scale-up trigger than default).

## Handling common follow-ups

- **"Why is it recommending X?"** — Read back the relevant rule from `references/thresholds.md`
  and the actual p95/max numbers from `report.json` that triggered it. Don't just restate the
  recommendation.
- **"What would the new tier cost?"** — The script doesn't fetch pricing (Atlas doesn't expose a
  stable public pricing API). Point the user to the Atlas UI pricing calculator or their billing
  page instead of guessing a number.
- **"Apply this change"** — Out of scope for this skill by design (read-only). If the user wants
  it automated, that's a different task: modifying the cluster via
  `PATCH /groups/{groupId}/clusters/{clusterName}` — flag the risk (in-place tier changes trigger
  a rolling restart) and confirm explicitly before ever writing that code for them.
