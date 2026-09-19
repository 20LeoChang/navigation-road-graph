# v4.0 migration — exact deployment order

This upgrade changes only the **custom car** routing backend. The existing motorcycle `/route` logic remains in the Worker file and is not changed by v4.

## 1. Update the GitHub graph repository

Replace/add these files in your existing `navigation-road-graph` repository:

- `.github/workflows/build-taiwan-road-graph.yml`
- `scripts/build_taiwan_graph.py`
- `scripts/build_v4_overlay.py` **(new)**
- `scripts/upload_r2.py`
- `requirements.txt`
- `README.md`

Your existing GitHub Secrets remain the same. Do not change or paste them into code.

## 2. Run the workflow manually once

GitHub → Actions → **Build Taiwan ROAD_GRAPH** → **Run workflow**.

The workflow now creates both:

```text
graph/v1/TW/*.json
graph/v4/TW/overlay.json
```

The v1 graph is still used for exact local/detail routing. The v4 overlay is the precomputed nationwide hierarchy.

## 3. Check R2

Cloudflare → R2 → bucket `navigation`.

Confirm this object exists:

```text
graph/v4/TW/overlay.json
```

Do not deploy v4 as the primary test until this object exists. The supplied Worker can fall back to v3.7.1 if the object is missing, but then you are not testing the new hierarchy.

## 4. Deploy the Worker

Use:

```text
worker/Worker_v4.0.0_hierarchical_overlay.txt
```

Existing bindings stay the same:

- `ROAD_GRAPH` → R2 bucket `navigation`
- `SIGNALS` → R2 bucket `navigation-signals`
- `TRAFFIC_KV` → current KV
- existing HERE / TDX / Google secrets remain unchanged

No new Cloudflare binding is required for v4.0.

## 5. Health check

Open `/health` and confirm:

```json
{
  "version": "4.0.0",
  "routingEngine": "hierarchical-overlay-v4",
  "roadGraphOverlayKey": "graph/v4/TW/overlay.json"
}
```

## 6. Test custom car routing

Use the existing endpoint unchanged:

```text
/route/custom-car?start=25.036810,121.53822&end=25.00694,121.49416&traffic=1&signals=0&postsignals=1&debug=0
```

A real v4 response should contain:

```json
"version":"4.0.0",
"engine":"custom-car-hierarchical-overlay-v4.0.0",
"hierarchyMode":"precomputed-major-road-overlay + single-detail-corridor",
"searchStateMode":"v4-overlay-then-single-node-detail"
```

If the response contains:

```json
"v4Fallback": true
```

then the v4 overlay was unavailable/invalid and the Worker fell back to the old v3.7.1 router.

## 7. Recommended regression tests

Run the same coordinates you have already used:

- Daan → Zhonghe: `25.036810,121.53822` → `25.00694,121.49416`
- Hualien → Taipei: `23.9939181,121.5997981` → `25.0410,121.5784`
- Hualien → Taichung: `23.9939181,121.5997981` → `24.1376653,120.6861686`
- Hualien → mountain destinations you previously tested

For the first v4 validation, compare these fields:

```text
overlay.expansions
expansions
loadedGraphTileCount
elapsedMs
trafficSnapshot.elapsedMs
```

The important architectural success condition is that the detailed A* stays inside the overlay-selected corridor instead of discovering Taiwan by loading arbitrary R2 tiles.
