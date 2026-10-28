"""Build the drivable road graph used by bdrives from an OpenStreetMap .osm.pbf extract.

The repo ships a prebuilt Bangalore graph (bdrives/data/bangalore_roads.npz). Use this to refresh it with
newer OSM data or to build one for another city.

    pip install osmium
    # Karnataka / Tamil Nadu / Kerala etc. are in Geofabrik's "southern-zone" extract (~560 MB):
    curl -LO https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf
    python tools/build_graph.py southern-zone-latest.osm.pbf --bbox 12.75,13.27,77.35,77.88 \
        --out bdrives/data/bangalore_roads.npz

For another city: pass its bounding box (lat_min,lat_max,lng_min,lng_max) and a new --out, then set BBOX,
AIRPORT and GRAPH in bdrives/config.py. Peak memory is ~2-3 GB for a state-size extract.

Road data © OpenStreetMap contributors, available under the Open Database License (ODbL).
"""
import argparse

import numpy as np
import osmium

DRIVE = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
         "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "road"}
NO_ACCESS = {"no", "private"}
SPEED = {"motorway": 80, "trunk": 60, "primary": 50, "secondary": 40, "tertiary": 35, "unclassified": 30,
         "residential": 25, "living_street": 15, "road": 30}           # km/h when no maxspeed tag


class Ways(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.ways, self.bbox = [], bbox

    def way(self, w):
        hw = w.tags.get("highway")
        if hw not in DRIVE or w.tags.get("access") in NO_ACCESS or w.tags.get("motor_vehicle") in NO_ACCESS \
                or w.tags.get("motorcar") in NO_ACCESS or w.tags.get("area") == "yes":
            return
        try:
            pts = [(n.ref, n.location.lon, n.location.lat) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        lat0, lat1, lng0, lng1 = self.bbox
        if not any(lng0 <= x <= lng1 and lat0 <= y <= lat1 for _, x, y in pts):
            return
        ow = w.tags.get("oneway", "")
        oneway = 1 if ow in ("yes", "true", "1") else -1 if ow == "-1" else 0
        if not oneway and (hw == "motorway" or w.tags.get("junction") in ("roundabout", "circular")) and ow != "no":
            oneway = 1
        ms = (w.tags.get("maxspeed") or "").split()
        ms = float(ms[0]) if ms and ms[0].replace(".", "", 1).isdigit() else None
        self.ways.append((pts, hw, w.tags.get("name", ""), oneway, ms))


def hav_m(lon, lat):
    lon, lat = np.radians(lon), np.radians(lat)
    a = np.sin(np.diff(lat) / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lon) / 2) ** 2
    return 6371000 * 2 * np.arcsin(np.sqrt(a))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("--bbox", required=True, help="lat_min,lat_max,lng_min,lng_max")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    h = Ways(tuple(map(float, args.bbox.split(","))))
    h.apply_file(args.pbf, locations=True, idx="flex_mem")
    print(f"{len(h.ways):,} drivable ways touching the box")

    use = {}
    for pts, *_ in h.ways:
        refs = [p[0] for p in pts]
        for r in refs + [refs[0], refs[-1]]:
            use[r] = use.get(r, 0) + 1
    junction = {r for r, c in use.items() if c >= 2}

    node, lat, lng = {}, [], []
    u, v, length, tt, geo, off, hw_l, name_l = [], [], [], [], [], [0], [], []

    def nid(ref, x, y):
        if ref not in node:
            node[ref] = len(lat)
            lat.append(y)
            lng.append(x)
        return node[ref]

    for pts, hw, name, oneway, ms in h.ways:
        speed = ms or SPEED.get(hw.replace("_link", ""), 30) * (0.8 if hw.endswith("_link") else 1)
        start = 0
        for i in range(1, len(pts)):
            if pts[i][0] in junction or i == len(pts) - 1:
                piece = pts[start:i + 1]
                xs, ys = np.array([p[1] for p in piece]), np.array([p[2] for p in piece])
                L = float(hav_m(xs, ys).sum())
                if L > 0:
                    a, b = nid(*piece[0]), nid(*piece[-1])
                    coords = list(zip(xs.tolist(), ys.tolist()))
                    for s, e, c in ([(a, b, coords)] if oneway >= 0 else []) + ([(b, a, coords[::-1])] if oneway <= 0 else []):
                        u.append(s); v.append(e); length.append(L); tt.append(L / (speed / 3.6))
                        geo.extend(c); off.append(len(geo)); hw_l.append(hw); name_l.append(name)
                start = i
    hw_names = sorted(set(hw_l))
    names = sorted(set(name_l))
    hi, ni = {h_: i for i, h_ in enumerate(hw_names)}, {n: i for i, n in enumerate(names)}
    np.savez_compressed(args.out, lat=np.array(lat), lng=np.array(lng), u=np.array(u, np.int32), v=np.array(v, np.int32),
                        length=np.array(length, np.float32), tt=np.array(tt, np.float32), geo=np.array(geo, np.float32),
                        geo_off=np.array(off, np.int64), highway=np.array([hi[x] for x in hw_l], np.uint8),
                        highway_names=np.array(hw_names), name_id=np.array([ni[x] for x in name_l], np.int32),
                        names=np.array(names))
    print(f"saved {args.out}: {len(lat):,} junctions, {len(u):,} directed road segments")


if __name__ == "__main__":
    main()
