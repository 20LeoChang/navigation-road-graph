#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import struct
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
GEOMETRY_CHUNK_EDGES = 4096

TOPO_MAGIC = b"TWV41CSR"
GEO_MAGIC = b"TWV41GEO"
HEADER_BYTES = 64
GEO_HEADER_BYTES = 32


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


def cell_xy(lat, lon):
    y = math.floor((lat + 90.0) / CELL_DEG)
    x = math.floor((lon + 180.0) / CELL_DEG)
    return y, x


def cell_id(lat, lon):
    y, x = cell_xy(lat, lon)
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


def _pack_u32(values):
    return struct.pack("<" + "I" * len(values), *values) if values else b""


def _pack_i32(values):
    return struct.pack("<" + "i" * len(values), *values) if values else b""


def write_topology(path: Path, lat_e5, lon_e5, first_edge, edge_to, edge_cost_ds, edge_dist_m):
    node_count = len(lat_e5)
    edge_count = len(edge_to)
    header = bytearray(HEADER_BYTES)
    header[:8] = TOPO_MAGIC
    struct.pack_into("<IIIIII", header, 8, 1, node_count, edge_count, 100000, 10, GEOMETRY_CHUNK_EDGES)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(header)
        f.write(_pack_i32(lat_e5))
        f.write(_pack_i32(lon_e5))
        f.write(_pack_u32(first_edge))
        f.write(_pack_u32(edge_to))
        f.write(_pack_u32(edge_cost_ds))
        f.write(_pack_u32(edge_dist_m))


def write_geometry_chunks(out_dir: Path, edge_shapes):
    geo_dir = out_dir / "geometry"
    geo_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    for start in range(0, len(edge_shapes), GEOMETRY_CHUNK_EDGES):
        rows = edge_shapes[start:start + GEOMETRY_CHUNK_EDGES]
        offsets = [0]
        coords = []
        for shp in rows:
            coords.extend(shp)
            offsets.append(len(coords))

        chunk_id = start // GEOMETRY_CHUNK_EDGES
        name = f"{chunk_id:05d}.bin"
        path = geo_dir / name

        header = bytearray(GEO_HEADER_BYTES)
        header[:8] = GEO_MAGIC
        struct.pack_into("<IIIII", header, 8, 1, start, len(rows), 100000, len(coords))

        with path.open("wb") as f:
            f.write(header)
            f.write(_pack_u32(offsets))
            f.write(_pack_i32(coords))

        chunks.append({
            "id": chunk_id,
            "startEdge": start,
            "edgeCount": len(rows),
            "bytes": path.stat().st_size,
            "key": f"geometry/{name}",
        })
    return chunks


def build_overlay(pbf: Path, out_dir: Path, location_index: str):
    print("[v4.1 1/5] membership pass")
    p1 = MembershipPass()
    p1.apply_file(str(pbf), locations=False)
    print(f"  major ways={p1.way_count:,} memberships={len(p1.memberships):,}")

    print("[v4.1 2/5] overlay geometry pass")
    p2 = OverlayPass(p1.memberships, p1.endpoints)
    p2.apply_file(str(pbf), locations=True, idx=location_index)
    print(f"  superedges={len(p2.edges):,} anchor nodes={len(p2.node_coords):,}")

    print("[v4.1 3/5] spatially ordered compact CSR")
    # Spatial ordering is deliberate: geometry edge chunks become geographically local,
    # so a route normally touches only a small number of R2 geometry objects.
    node_ids = sorted(
        p2.node_coords.keys(),
        key=lambda nid: (
            cell_xy(p2.node_coords[nid][0], p2.node_coords[nid][1]),
            round(p2.node_coords[nid][0], 5),
            round(p2.node_coords[nid][1], 5),
            nid,
        ),
    )
    idx_of = {nid: i for i, nid in enumerate(node_ids)}
    lat_e5 = [int(round(p2.node_coords[n][0] * 1e5)) for n in node_ids]
    lon_e5 = [int(round(p2.node_coords[n][1] * 1e5)) for n in node_ids]

    by_from = defaultdict(list)
    for e in p2.edges:
        if e["from"] in idx_of and e["to"] in idx_of:
            by_from[idx_of[e["from"]]].append(e)

    first_edge = [0]
    edge_to, edge_cost_ds, edge_dist_m, edge_shapes = [], [], [], []
    for i in range(len(node_ids)):
        rows = by_from.get(i, [])
        rows.sort(key=lambda e: (idx_of[e["to"]], e["costSec"], e["lengthM"]))
        for e in rows:
            edge_to.append(idx_of[e["to"]])
            edge_cost_ds.append(max(1, int(round(e["costSec"] * 10))))
            edge_dist_m.append(max(1, int(round(e["lengthM"]))))
            shp = []
            for lat, lon in e["shape"]:
                shp.extend([int(round(lat * 1e5)), int(round(lon * 1e5))])
            edge_shapes.append(shp)
        first_edge.append(len(edge_to))

    cells = defaultdict(list)
    for i, (la, lo) in enumerate(zip(lat_e5, lon_e5)):
        cells[cell_id(la / 1e5, lo / 1e5)].append(i)

    out_dir.mkdir(parents=True, exist_ok=True)
    topo_path = out_dir / "topology.bin"
    write_topology(topo_path, lat_e5, lon_e5, first_edge, edge_to, edge_cost_ds, edge_dist_m)

    print("[v4.1 4/5] partitioned geometry")
    chunks = write_geometry_chunks(out_dir, edge_shapes)

    index = {
        "schemaVersion": 41,
        "engine": "taiwan-major-road-overlay-csr-binary",
        "source": "Geofabrik taiwan-latest.osm.pbf / OpenStreetMap",
        "cellDeg": CELL_DEG,
        "coordScale": 100000,
        "costScale": 10,
        "topologyFile": "topology.bin",
        "geometryPrefix": "geometry",
        "geometryChunkEdges": GEOMETRY_CHUNK_EDGES,
        "cellIndex": dict(cells),
        "stats": {
            "majorWayCount": p2.accepted_ways,
            "nodeCount": len(node_ids),
            "edgeCount": len(edge_to),
            "cellCount": len(cells),
            "geometryChunkCount": len(chunks),
        },
        "geometryChunks": chunks,
    }
    index_path = out_dir / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print("[v4.1 5/5] done")
    total_geo = sum(c["bytes"] for c in chunks)
    print(json.dumps({
        "index": str(index_path),
        "topology": str(topo_path),
        "nodeCount": len(node_ids),
        "edgeCount": len(edge_to),
        "geometryChunks": len(chunks),
        "indexMiB": round(index_path.stat().st_size / 1024 / 1024, 3),
        "topologyMiB": round(topo_path.stat().st_size / 1024 / 1024, 3),
        "geometryMiB": round(total_geo / 1024 / 1024, 3),
        "totalMiB": round((index_path.stat().st_size + topo_path.stat().st_size + total_geo) / 1024 / 1024, 3),
    }))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--out-dir", default="dist/graph/v4.1/TW")
    ap.add_argument("--location-index", default="flex_mem")
    args = ap.parse_args()
    build_overlay(Path(args.pbf), Path(args.out_dir), args.location_index)


if __name__ == "__main__":
    main()
