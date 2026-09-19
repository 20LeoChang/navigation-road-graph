# Taiwan ROAD_GRAPH v4 hierarchical pipeline

This pipeline keeps the existing `graph/v1/TW/*.json` detailed car graph **and** builds a new precomputed `graph/v4/TW/overlay.json` hierarchy for `/route/custom-car`.

## Why v4 exists

The v3 Worker dynamically loaded many detailed JSON tiles and ran ordinary A* directly on the dense Taiwan road graph. Dense Taipei/New Taipei routes and long/cross-mountain routes could therefore hit Cloudflare Worker Error 1102 (CPU/memory resource limit).

v4 moves the expensive nationwide topology work into GitHub Actions:

1. Download Taiwan OSM PBF from Geofabrik.
2. Build the existing detailed `graph/v1/TW` tiles.
3. Run a second OSM pass for major drivable roads (`motorway` through `tertiary`).
4. Contract road geometry between junction/end nodes into a compact directed overlay.
5. Store the overlay as CSR arrays plus a 0.10-degree spatial index.
6. Upload `graph/v4/TW/overlay.json` to the same `navigation` R2 bucket.

At query time the v4 Worker:

- snaps start/end using the existing detailed graph;
- runs a very small A* on the precomputed overlay;
- converts the overlay result into a geographic corridor;
- runs **one** detailed local A* inside that corridor;
- then uses the existing HERE + TDX traffic matcher and existing signal/ETA logic.

The motorcycle `/route` endpoint is not changed.

## GitHub Secrets

Keep the same secrets you already configured:

- `R2_ENDPOINT`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

Do not commit secrets into the repository.

## Files

- `scripts/build_taiwan_graph.py` – current v1 detailed graph builder.
- `scripts/build_v4_overlay.py` – v4 hierarchical overlay builder.
- `scripts/upload_r2.py` – R2 uploader.
- `.github/workflows/build-taiwan-road-graph.yml` – builds and uploads both v1 and v4.

## Expected R2 keys

```text
graph/v1/TW/<0.05-degree-tile>.json
graph/v1/TW/_manifest.json
graph/v4/TW/overlay.json
```

## Deployment order

1. Push these pipeline files to your existing `navigation-road-graph` repository.
2. Run **Build Taiwan ROAD_GRAPH** manually once.
3. Confirm `graph/v4/TW/overlay.json` exists in R2 bucket `navigation`.
4. Deploy the supplied v4 Worker.
5. Test `/route/custom-car`; `/health` should report the v4 overlay key.

The v4 Worker includes a controlled fallback to the existing v3.7.1 car router if the overlay object has not been uploaded yet, so deployment order is safer.
