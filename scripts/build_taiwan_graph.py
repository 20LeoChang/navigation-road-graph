#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import osmium

TILE_DEG = 0.05
SCHEMA_VERSION = 1
COUNTRY = "TW"
PREFIX = "graph/v1/TW"

CAR_HIGHWAYS = {
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "unclassified", "residential",
    "living_street", "service", "road",
}

DEFAULT_SPEED_KPH = {
    "motorway": 90,
    "motorway_link": 50,
    "trunk": 70,
    "trunk_link": 45,
    "primary": 55,
    "primary_link": 40,
    "secondary": 45,
    "secondary_link": 35,
    "tertiary": 40,
    "tertiary_link": 30,
    "unclassified": 35,
    "residential": 30,
    "living_street": 15,
    "service": 20,
    "road": 30,
}

HARD_DENIED = {"no", "private", "agricultural", "forestry"}


def floor_graph_coord(v: float) -> float:
    n = math.floor((float(v) + 1e-10) / TILE_DEG)
    return n * TILE_DEG


def tile_id_for(lat: float, lon: float) -> str:
    south = floor_graph_coord(lat)
    west = floor_graph_coord(lon)
    return f"{south:.2f}_{west:.2f}"


def tile_bounds(tile_id: str) -> dict:
    s, w = tile_id.split("_", 1)
    south, west = float(s), float(w)
    return {
        "south": south,
        "west": west,
        "north": south + TILE_DEG,
        "east": west + TILE_DEG,
    }


def haversine_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def parse_maxspeed_kph(raw: str | None) -> float | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    import re
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    v = float(m.group(1))
    if "mph" in s:
        v *= 1.609344
    if v < 5 or v > 160:
        return None
    return round(v, 1)


def denied_for_car(tags: dict) -> bool:
    for k in ("access", "vehicle", "motor_vehicle", "motorcar"):
        if (tags.get(k) or "").strip().lower() in HARD_DENIED:
            return True

    if (tags.get("highway") or "").strip().lower() == "service":
        if (tags.get("service") or "").strip().lower() == "emergency_access":
            return True
    return False


def is_drivable(tags: dict) -> bool:
    h = (tags.get("highway") or "").strip().lower()
    if h not in CAR_HIGHWAYS:
        return False
    if denied_for_car(tags):
        return False
    status = (tags.get("status") or tags.get("construction") or "").strip().lower()
    if h == "construction" or status == "construction":
        return False
    return True


def way_direction(tags: dict) -> int:
    # 1 forward, -1 reverse, 0 both
    oneway = (tags.get("oneway") or "").strip().lower()
    junction = (tags.get("junction") or "").strip().lower()
    highway = (tags.get("highway") or "").strip().lower()

    if oneway in {"-1", "reverse"}:
        return -1
    if oneway in {"yes", "1", "true"}:
        return 1
    if junction == "roundabout" or highway in {"motorway", "motorway_link"}:
        if oneway in {"no", "0", "false"}:
            return 0
        return 1
    return 0


def default_speed(tags: dict) -> float:
    explicit = parse_maxspeed_kph(tags.get("maxspeed"))
    if explicit is not None:
        return explicit
    return float(DEFAULT_SPEED_KPH.get((tags.get("highway") or "").lower(), 30))


class LRUFiles:
    def __init__(self, root: Path, max_open: int = 64):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_open = max_open
        self._files: OrderedDict[str, object] = OrderedDict()

    def write_json_line(self, tile_id: str, obj: dict) -> None:
        f = self._files.get(tile_id)
        if f is None:
            path = self.root / f"{tile_id}.ndjson"
            f = path.open("a", encoding="utf-8")
            self._files[tile_id] = f
        else:
            self._files.move_to_end(tile_id)

        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

        while len(self._files) > self.max_open:
            _, old = self._files.popitem(last=False)
            old.close()

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()


class GraphHandler(osmium.SimpleHandler):
    def __init__(self, spool: LRUFiles):
        super().__init__()
        self.spool = spool
        self.ways_seen = 0
        self.ways_accepted = 0
        self.edges_emitted = 0
        self.bad_locations = 0

    def way(self, w):
        self.ways_seen += 1
        tags = {k: v for k, v in w.tags}
        if not is_drivable(tags):
            return

        nodes = list(w.nodes)
        if len(nodes) < 2:
            return

        highway = (tags.get("highway") or "").lower()
        speed = default_speed(tags)
        direction = way_direction(tags)
        name = (tags.get("name") or tags.get("ref") or "").strip()[:120]
        way_id = str(w.id)

        emitted = False

        for i in range(len(nodes) - 1):
            na, nb = nodes[i], nodes[i + 1]
            if not na.location.valid() or not nb.location.valid():
                self.bad_locations += 1
                continue

            a = (str(na.ref), float(na.location.lat), float(na.location.lon))
            b = (str(nb.ref), float(nb.location.lat), float(nb.location.lon))

            if direction >= 0:
                emitted |= self._emit(a, b, highway, speed, name, way_id)
            if direction <= 0:
                emitted |= self._emit(b, a, highway, speed, name, way_id)

        if emitted:
            self.ways_accepted += 1

    def _emit(self, a, b, highway, speed, name, way_id) -> bool:
        from_id, a_lat, a_lon = a
        to_id, b_lat, b_lon = b
        length_m = haversine_m(a_lat, a_lon, b_lat, b_lon)
        if not (0.2 < length_m < 10000):
            return False

        tile_id = tile_id_for(a_lat, a_lon)
        self.spool.write_json_line(tile_id, {
            "from": from_id,
            "to": to_id,
            "a": [a_lat, a_lon],
            "b": [b_lat, b_lon],
            "lengthM": round(length_m, 1),
            "speedKph": speed,
            "highway": highway,
            "name": name,
            "osmWayId": way_id,
        })
        self.edges_emitted += 1
        return True


def finalize_tile(spool_file: Path, out_root: Path) -> dict:
    tile_id = spool_file.stem
    nodes: Dict[str, Tuple[float, float]] = {}
    edge_keys = set()
    edges: List[list] = []

    with spool_file.open("r", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            fr, to = str(r["from"]), str(r["to"])
            a_lat, a_lon = r["a"]
            b_lat, b_lon = r["b"]
            nodes.setdefault(fr, (a_lat, a_lon))
            nodes.setdefault(to, (b_lat, b_lon))

            row = [
                fr,
                to,
                float(r["lengthM"]),
                float(r["speedKph"]),
                r["highway"],
                r["name"],
                str(r["osmWayId"]),
            ]

            # Same practical dedupe rule as the Worker loader.
            ek = (fr, to, str(r["osmWayId"]), round(float(r["lengthM"]), 1))
            if ek in edge_keys:
                continue
            edge_keys.add(ek)
            edges.append(row)

    node_rows = [[nid, lat, lon] for nid, (lat, lon) in nodes.items()]
    node_rows.sort(key=lambda x: int(x[0]) if x[0].lstrip("-").isdigit() else x[0])
    edges.sort(key=lambda x: (
        int(x[0]) if x[0].lstrip("-").isdigit() else x[0],
        int(x[1]) if x[1].lstrip("-").isdigit() else x[1],
    ))

    bounds = tile_bounds(tile_id)
    obj = {
        "schemaVersion": SCHEMA_VERSION,
        "engine": "osm-car-directed-graph",
        "country": COUNTRY,
        "tileId": tile_id,
        "bounds": bounds,
        "paddedBounds": bounds,
        "builtAt": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "source": "Geofabrik taiwan-latest.osm.pbf / OpenStreetMap",
        "ownership": "edge stored in tile containing FROM node",
        "nodeColumns": ["id", "lat", "lon"],
        "edgeColumns": ["from", "to", "lengthM", "defaultSpeedKph", "highway", "name", "osmWayId"],
        "nodes": node_rows,
        "edges": edges,
        "stats": {
            "nodeCount": len(node_rows),
            "edgeCount": len(edges),
            "missingNodePairs": 0,
        },
    }

    out_path = out_root / f"{tile_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return {
        "tileId": tile_id,
        "file": out_path.name,
        "nodeCount": len(node_rows),
        "edgeCount": len(edges),
        "bytes": out_path.stat().st_size,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--out", default="dist/graph/v1/TW")
    ap.add_argument("--work", default=".graph-work")
    ap.add_argument("--location-index", default="flex_mem",
                    help="pyosmium location index, default flex_mem")
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()

    pbf = Path(args.pbf)
    out_root = Path(args.out)
    work = Path(args.work)
    spool_dir = work / "spool"

    if not pbf.exists():
        print(f"PBF not found: {pbf}", file=sys.stderr)
        return 2

    if work.exists():
        shutil.rmtree(work)
    spool_dir.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    spool = LRUFiles(spool_dir)
    handler = GraphHandler(spool)

    print(f"[1/3] Reading {pbf} with pyosmium location index={args.location_index}")
    try:
        handler.apply_file(str(pbf), locations=True, idx=args.location_index)
    finally:
        spool.close()

    spool_files = sorted(spool_dir.glob("*.ndjson"))
    print(f"[2/3] Finalizing {len(spool_files)} non-empty graph tiles")

    manifest_tiles = []
    total_nodes = 0
    total_edges = 0
    total_bytes = 0

    for i, sf in enumerate(spool_files, 1):
        s = finalize_tile(sf, out_root)
        manifest_tiles.append(s)
        total_nodes += s["nodeCount"]
        total_edges += s["edgeCount"]
        total_bytes += s["bytes"]
        if i % 100 == 0 or i == len(spool_files):
            print(f"  {i}/{len(spool_files)} tiles")

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "country": COUNTRY,
        "prefix": PREFIX,
        "tileDeg": TILE_DEG,
        "source": "https://download.geofabrik.de/asia/taiwan-latest.osm.pbf",
        "waysSeen": handler.ways_seen,
        "waysAccepted": handler.ways_accepted,
        "edgesEmittedBeforeTileDedupe": handler.edges_emitted,
        "badLocations": handler.bad_locations,
        "tileCount": len(manifest_tiles),
        "nodeRowsAcrossTiles": total_nodes,
        "edgeCount": total_edges,
        "totalJsonBytes": total_bytes,
        "tiles": manifest_tiles,
    }
    (out_root / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("[3/3] Done")
    print(json.dumps({
        "tileCount": manifest["tileCount"],
        "edgeCount": total_edges,
        "jsonMiB": round(total_bytes / 1024 / 1024, 1),
        "out": str(out_root),
    }, ensure_ascii=False))

    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
