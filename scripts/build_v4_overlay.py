#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import osmium

OVERLAY_HIGHWAYS = {
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
}

DEFAULT_SPEED_KPH = {
    "motorway": 90, "motorway_link": 50,
    "trunk": 70, "trunk_link": 45,
    "primary": 55, "primary_link": 40,
    "secondary": 45, "secondary_link": 35,
    "tertiary": 40, "tertiary_link": 30,
}

HARD_DENIED = {"no", "private", "agricultural", "forestry"}
CELL_DEG = 0.10
SHAPE_SAMPLE_M = 1200.0


def haversine_m(a_lat, a_lon, b_lat, b_lon):
    r = 6371008.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def parse_maxspeed_kph(raw):
    import re
    s = (raw or "").strip().lower()
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    v = float(m.group(1))
    if "mph" in s:
        v *= 1.609344
    if v < 5 or v > 160:
        return None
    return v


def denied_for_car(tags):
    for k in ("access", "vehicle", "motor_vehicle", "motorcar"):
        if (tags.get(k) or "").strip().lower() in HARD_DENIED:
            return True
    if (tags.get("highway") or "").strip().lower() == "service":
        if (tags.get("service") or "").strip().lower() == "emergency_access":
            return True
    return False


def accepted(tags):
    return (tags.get("highway") or "").strip().lower() in OVERLAY_HIGHWAYS and not denied_for_car(tags)


def way_direction(tags):
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


def speed_kph(tags):
    v = parse_maxspeed_kph(tags.get("maxspeed"))
    if v is not None:
        return v
    return float(DEFAULT_SPEED_KPH.get((tags.get("highway") or "").lower(), 35))


def cell_id(lat, lon):
    y = math.floor((lat + 90.0) / CELL_DEG)
    x = math.floor((lon + 180.0) / CELL_DEG)
    return f"{y}:{x}"


class MembershipPass(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.memberships = defaultdict(int)
        self.endpoints = set()
        self.way_count = 0

    def way(self, w):
        tags = {k: v for k, v in w.tags}
        if not accepted(tags):
            return
        refs = [int(n.ref) for n in w.nodes]
        if len(refs) < 2:
            return
        self.way_count += 1
        seen = set()
        for ref in refs:
            if ref not in seen:
                self.memberships[ref] += 1
                seen.add(ref)
        self.endpoints.add(refs[0])
        self.endpoints.add(refs[-1])


class OverlayPass(osmium.SimpleHandler):
    def __init__(self, memberships, endpoints):
        super().__init__()
        self.memberships = memberships
        self.endpoints = endpoints
        self.node_coords: Dict[int, Tuple[float, float]] = {}
        self.edges = []
        self.accepted_ways = 0

    def _is_anchor(self, ref, is_endpoint=False):
        return is_endpoint or ref in self.endpoints or self.memberships.get(ref, 0) > 1

    def _sample_shape(self, pts):
        if len(pts) <= 2:
            return pts
        out = [pts[0]]
        carried = 0.0
        prev = pts[0]
        for p in pts[1:-1]:
            d = haversine_m(prev[0], prev[1], p[0], p[1])
            carried += d
            if carried >= SHAPE_SAMPLE_M:
                out.append(p)
                carried = 0.0
            prev = p
        out.append(pts[-1])
        return out

    def _emit_segment(self, refs, pts, highway, speed, way_id, direction):
        if len(refs) < 2:
            return
        dist = 0.0
        for a, b in zip(pts, pts[1:]):
            dist += haversine_m(a[0], a[1], b[0], b[1])
        if not (dist > 0.2):
            return
        sec = dist / max(1.0, speed * 1000.0 / 3600.0)
        shape = self._sample_shape(pts)

        fr, to = refs[0], refs[-1]
        self.node_coords.setdefault(fr, pts[0])
        self.node_coords.setdefault(to, pts[-1])

        base = {
            "lengthM": round(dist, 1),
            "costSec": round(sec, 2),
            "highway": highway,
            "wayId": str(way_id),
        }
        if direction >= 0:
            self.edges.append({"from": fr, "to": to, "shape": shape, **base})
        if direction <= 0:
            self.edges.append({"from": to, "to": fr, "shape": list(reversed(shape)), **base})

    def way(self, w):
        tags = {k: v for k, v in w.tags}
        if not accepted(tags):
            return
        nodes = list(w.nodes)
        if len(nodes) < 2:
            return
        if any(not n.location.valid() for n in nodes):
            return

        highway = (tags.get("highway") or "").lower()
        speed = speed_kph(tags)
        direction = way_direction(tags)
        self.accepted_ways += 1

        refs = [int(n.ref) for n in nodes]
        pts = [(float(n.location.lat), float(n.location.lon)) for n in nodes]

        seg_refs = [refs[0]]
        seg_pts = [pts[0]]
        for i in range(1, len(nodes)):
            seg_refs.append(refs[i])
            seg_pts.append(pts[i])
            anchor = self._is_anchor(refs[i], is_endpoint=(i == len(nodes)-1))
            if anchor:
                self._emit_segment(seg_refs, seg_pts, highway, speed, w.id, direction)
                seg_refs = [refs[i]]
                seg_pts = [pts[i]]


def build_overlay(pbf: Path, out_path: Path, location_index: str):
    print("[v4 1/4] membership pass")
    p1 = MembershipPass()
    p1.apply_file(str(pbf), locations=False)
    print(f"  major ways={p1.way_count:,} memberships={len(p1.memberships):,}")

    print("[v4 2/4] overlay geometry pass")
    p2 = OverlayPass(p1.memberships, p1.endpoints)
    p2.apply_file(str(pbf), locations=True, idx=location_index)
    print(f"  superedges={len(p2.edges):,} anchor nodes={len(p2.node_coords):,}")

    print("[v4 3/4] compact CSR")
    node_ids = sorted(p2.node_coords.keys())
    idx_of = {nid: i for i, nid in enumerate(node_ids)}
    lat_e5 = [int(round(p2.node_coords[n][0] * 1e5)) for n in node_ids]
    lon_e5 = [int(round(p2.node_coords[n][1] * 1e5)) for n in node_ids]

    by_from = defaultdict(list)
    for e in p2.edges:
        if e["from"] not in idx_of or e["to"] not in idx_of:
            continue
        by_from[idx_of[e["from"]]].append(e)

    first_edge = [0]
    edge_to, edge_cost_ds, edge_dist_m, edge_hwy, edge_shapes = [], [], [], [], []
    hwy_codes = {h: i for i, h in enumerate(sorted(OVERLAY_HIGHWAYS))}

    for i in range(len(node_ids)):
        rows = by_from.get(i, [])
        rows.sort(key=lambda e: (idx_of[e["to"]], e["costSec"], e["lengthM"]))
        for e in rows:
            edge_to.append(idx_of[e["to"]])
            edge_cost_ds.append(max(1, int(round(e["costSec"] * 10))))
            edge_dist_m.append(max(1, int(round(e["lengthM"]))))
            edge_hwy.append(hwy_codes[e["highway"]])
            shp = []
            for lat, lon in e["shape"]:
                shp.extend([int(round(lat * 1e5)), int(round(lon * 1e5))])
            edge_shapes.append(shp)
        first_edge.append(len(edge_to))

    cells = defaultdict(list)
    for i, (la, lo) in enumerate(zip(lat_e5, lon_e5)):
        cells[cell_id(la / 1e5, lo / 1e5)].append(i)

    obj = {
        "schemaVersion": 4,
        "engine": "taiwan-major-road-overlay-csr",
        "source": "Geofabrik taiwan-latest.osm.pbf / OpenStreetMap",
        "cellDeg": CELL_DEG,
        "coordScale": 100000,
        "costScale": 10,
        "highwayCodes": {str(v): k for k, v in hwy_codes.items()},
        "nodeIds": [str(x) for x in node_ids],
        "latE5": lat_e5,
        "lonE5": lon_e5,
        "firstEdge": first_edge,
        "edgeTo": edge_to,
        "edgeCostDs": edge_cost_ds,
        "edgeDistM": edge_dist_m,
        "edgeHighway": edge_hwy,
        "edgeShapesE5": edge_shapes,
        "cellIndex": dict(cells),
        "stats": {
            "majorWayCount": p2.accepted_ways,
            "nodeCount": len(node_ids),
            "edgeCount": len(edge_to),
            "cellCount": len(cells),
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("[v4 4/4] done")
    print(json.dumps({
        "path": str(out_path),
        "nodeCount": len(node_ids),
        "edgeCount": len(edge_to),
        "MiB": round(out_path.stat().st_size / 1024 / 1024, 2),
    }))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--out", default="dist/graph/v4/TW/overlay.json")
    ap.add_argument("--location-index", default="flex_mem")
    args = ap.parse_args()
    build_overlay(Path(args.pbf), Path(args.out), args.location_index)


if __name__ == "__main__":
    main()
