"""Map-match car (and optionally motorbike) trips onto the road network and time every road segment.

For each trip: gather its points (Google's track + precise fixes when available), drop GPS spikes, thin to
one point per THIN_M metres, give each point its K_CAND nearest junctions, and pick the sequence of
junctions that is cheapest to drive over the whole trip (Viterbi over shortest-path travel times). Legs
that need an implausible detour, speed or gap are rejected. Each leg's time is spread over its roads in
proportion to their free-flow travel time. A second pass makes roads used often slightly cheaper, so close
calls go to the habitual route.

Writes tables `trips` (one row per trip, incl. the airport-cab flag) and `traversals` (one row per road
segment driven) to the SQLite file.
"""
import sqlite3
import time

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from . import config as C
from .geo import LocalXY, haversine_km


class Graph:
    def __init__(self, path=C.GRAPH):
        g = np.load(path, allow_pickle=False)
        self.lat, self.lng = g["lat"], g["lng"]
        self.u, self.v = g["u"], g["v"]
        self.length, self.tt = g["length"].astype(float), g["tt"].astype(float)
        self.geo, self.geo_off = g["geo"], g["geo_off"]
        self.highway, self.highway_names = g["highway"], list(g["highway_names"])
        self.name_id, self.names = g["name_id"], list(g["names"])
        self.proj = LocalXY((self.lat.min() + self.lat.max()) / 2)
        self.xy = np.c_[self.proj(self.lat, self.lng)]
        self.tree = cKDTree(self.xy)
        self.n = len(self.lat)
        order = np.lexsort((self.tt, self.v, self.u))
        first = np.r_[True, (np.diff(self.u[order]) != 0) | (np.diff(self.v[order]) != 0)]
        self.edge_of = order[first]
        self.arc = {(int(a), int(b)): int(e) for a, b, e in zip(self.u[self.edge_of], self.v[self.edge_of], self.edge_of)}
        self.set_weights(np.ones(len(self.u)))

    def set_weights(self, factor):
        w = np.maximum(self.tt[self.edge_of] * factor[self.edge_of], 0.01)
        self.W = csr_matrix((w, (self.u[self.edge_of], self.v[self.edge_of])), shape=(self.n, self.n))

    def edge_coords(self, e):
        return self.geo[self.geo_off[e]:self.geo_off[e + 1]]


def clean_trace(tr):
    """tr: DataFrame[t (epoch s), lat, lng] in time order. Remove duplicate times and speed spikes."""
    tr = tr.drop_duplicates("t").reset_index(drop=True)
    while len(tr) > 2:
        dt = tr.t.diff().clip(lower=1)
        kmh = (haversine_km(tr.lat.shift(), tr.lng.shift(), tr.lat, tr.lng) / (dt / 3600)).fillna(0)
        score = kmh + kmh.shift(-1).fillna(0)
        score.iloc[[0, -1]] = 0
        worst = score.idxmax()
        if kmh.iloc[worst] <= C.MAX_STEP_KMH and kmh.iloc[min(worst + 1, len(tr) - 1)] <= C.MAX_STEP_KMH:
            break
        tr = tr.drop(index=worst).reset_index(drop=True)
    return tr


def load_traces(con, modes, bbox=C.BBOX):
    lat0, lat1, lng0, lng1 = bbox
    a = pd.read_sql(f"SELECT seg_id, start_time, t, t_end, mode, distance_m, start_lat, start_lng, end_lat, end_lng "
                    f"FROM activities WHERE mode IN ({','.join('?' * len(modes))})", con, params=list(modes))
    inb = lambda la, ln: la.between(lat0, lat1) & ln.between(lng0, lng1)
    a = a[inb(a.start_lat, a.start_lng) & inb(a.end_lat, a.end_lng)].sort_values("t").reset_index(drop=True)
    p = pd.read_sql("SELECT t, lat, lng FROM path_points", con)
    try:
        r = pd.read_sql("SELECT t, lat, lng FROM raw_positions WHERE accuracy_m <= 30", con)
        p = pd.concat([p, r])
    except Exception:
        pass
    p = p.sort_values("t").drop_duplicates("t")
    i = np.searchsorted(a.t.values, p.t.values, side="right") - 1
    ok = (i >= 0) & (p.t.values <= a.t_end.values[i.clip(0)])
    by_trip = dict(list(p[ok].assign(k=i[ok]).groupby("k")))
    for k, trip in a.iterrows():
        parts = [pd.DataFrame({"t": [trip.t], "lat": [trip.start_lat], "lng": [trip.start_lng]}),
                 by_trip[k][["t", "lat", "lng"]] if k in by_trip else None,
                 pd.DataFrame({"t": [trip.t_end], "lat": [trip.end_lat], "lng": [trip.end_lng]})]
        yield trip, clean_trace(pd.concat(parts).sort_values("t").reset_index(drop=True))


def thin(xy):
    keep, last = [0], xy[0]
    for i in range(1, len(xy) - 1):
        if np.hypot(*(xy[i] - last)) >= C.THIN_M:
            keep.append(i)
            last = xy[i]
    keep.append(len(xy) - 1)
    return np.array(keep)


def match_trip(G, trip_id, tr):
    xy = np.c_[G.proj(tr.lat.values, tr.lng.values)]
    t = tr.t.values.astype(float)
    k = thin(xy)
    xy, t = xy[k], t[k]
    d, idx = G.tree.query(xy, k=C.K_CAND, distance_upper_bound=C.SNAP_M)
    keep = np.isfinite(d[:, 0])
    xy, t, d, idx = xy[keep], t[keep], d[keep], idx[keep]
    rows, legs, stats = [], [], {"ok": 0, "bad": 0}
    if len(t) < 2:
        return rows, legs, stats

    def flush(chunk):
        if len(chunk) < 2:
            return
        score, back = chunk[0][2].copy(), []
        for _, cands, emis, trans, _ in chunk[1:]:
            tot = score[:, None] + trans
            bi = tot.argmin(axis=0)
            score = tot[bi, np.arange(len(cands))] + emis
            back.append(bi)
        pick = [int(score.argmin())]
        for bi in reversed(back):
            pick.append(int(bi[pick[-1]]))
        pick = pick[::-1]
        for s in range(1, len(chunk)):
            i, cands, _, _, paths = chunk[s]
            a, b = chunk[s - 1][1][pick[s - 1]], cands[pick[s]]
            path = paths.get((pick[s - 1], pick[s]))
            if a == b or path is None:
                continue
            dt = t[i] - t[i - 1]
            straight = np.hypot(*(xy[i] - xy[i - 1]))
            L = G.length[path].sum()
            if L > max(C.MAX_DETOUR * straight, straight + 600) or L / dt * 3.6 > C.MAX_KMH:
                stats["bad"] += 1
                continue
            stats["ok"] += 1
            kmh = L / dt * 3.6
            ff = G.tt[path]
            secs = dt * ff / ff.sum()
            enter = t[i - 1] + np.r_[0, np.cumsum(secs)[:-1]]
            rows.extend((trip_id, int(e), te, s_, kmh, kmh < C.STOP_KMH) for e, s_, te in zip(path, secs, enter))
            legs.append(path)

    ok0 = np.isfinite(d[0])
    chunk = [(0, idx[0][ok0], d[0][ok0] / C.SNAP_M_PER_S, None, None)]
    for i in range(1, len(t)):
        oki = np.isfinite(d[i])
        ci, emis = idx[i][oki], d[i][oki] / C.SNAP_M_PER_S
        dt, prev = t[i] - t[i - 1], chunk[-1][1]
        straight = np.hypot(*(xy[i] - xy[i - 1]))
        trans, paths = np.full((len(prev), len(ci)), np.inf), {}
        if 0 < dt <= C.MAX_LEG_S:
            dist, pred = dijkstra(G.W, indices=prev, limit=120 + 3 * straight / 8.0, return_predecessors=True)
            for r, a in enumerate(prev):
                for c, b in enumerate(ci):
                    if a == b:
                        trans[r, c], paths[(r, c)] = 0.0, None
                    elif np.isfinite(dist[r, b]):
                        trans[r, c] = dist[r, b]
                        nodes = [b]
                        while nodes[-1] != a:
                            nodes.append(pred[r, nodes[-1]])
                        nodes.reverse()
                        paths[(r, c)] = [G.arc[(int(x), int(y))] for x, y in zip(nodes[:-1], nodes[1:])]
        if not np.isfinite(trans).any():
            stats["bad"] += dt > 0
            flush(chunk)
            chunk = [(i, ci, emis, None, None)]
            continue
        chunk.append((i, ci, emis, trans, paths))
    flush(chunk)
    return rows, legs, stats


def airport_cabs(con, trips):
    """A car leg to the airport followed by a flight within a few hours (or from the airport just after a
    flight landed) was almost certainly a cab. Same-day drop-offs / pick-ups stay as driving."""
    fl = pd.read_sql("SELECT t, t_end FROM activities WHERE mode = 'FLYING'", con)
    w = C.FLIGHT_WINDOW_H * 3600
    to_ap = haversine_km(trips.end_lat, trips.end_lng, *C.AIRPORT) <= C.AIRPORT_RADIUS_KM
    from_ap = haversine_km(trips.start_lat, trips.start_lng, *C.AIRPORT) <= C.AIRPORT_RADIUS_KM
    dep, arr = np.sort(fl.t.values), np.sort(fl.t_end.values)
    next_dep = lambda x: dep[np.searchsorted(dep, x)] if np.searchsorted(dep, x) < len(dep) else np.inf
    prev_arr = lambda x: arr[np.searchsorted(arr, x) - 1] if np.searchsorted(arr, x) > 0 else -np.inf
    return np.array([(ta and next_dep(e) - e <= w) or (fa and s - prev_arr(s) <= w)
                     for ta, fa, s, e in zip(to_ap, from_ap, trips.t, trips.t_end)])


def run(db, bikes=True, passes=2, graph=C.GRAPH, progress=None):
    """Match all car (and bike) trips inside the city box. progress(done, total, message) is optional."""
    con = sqlite3.connect(db)
    G = Graph(graph)
    kinds = [("car", C.CAR_MODES)] + ([("bike", C.BIKE_MODES)] if bikes else [])
    jobs = [(kind, trip, tr) for kind, modes in kinds for trip, tr in load_traces(con, modes)]
    total = len(jobs) * passes
    factor = np.ones(len(G.u))
    t0 = time.time()
    for p in range(1, passes + 1):
        G.set_weights(factor)
        rows, trips, usage = [], [], {}
        for n, (kind, trip, tr) in enumerate(jobs):
            r, legs, st = match_trip(G, int(trip.seg_id), tr)
            rows += [(kind,) + x for x in r]
            for e in {e for leg in legs for e in leg}:
                usage[e] = usage.get(e, 0) + 1
            trips.append({"trip_id": int(trip.seg_id), "kind": kind, "mode": trip["mode"], "start_time": trip.start_time,
                          "t": trip.t, "t_end": trip.t_end, "start_lat": trip.start_lat, "start_lng": trip.start_lng,
                          "end_lat": trip.end_lat, "end_lng": trip.end_lng, "google_m": trip.distance_m,
                          "matched_m": float(sum(G.length[l].sum() for l in legs)), "legs_ok": st["ok"], "legs_bad": st["bad"]})
            if progress and n % 25 == 0:
                progress((p - 1) * len(jobs) + n, total, f"pass {p}/{passes}: trip {n:,} of {len(jobs):,}")
        if p < passes:
            factor = np.ones(len(G.u))
            used, cnt = np.array(list(usage)), np.array(list(usage.values()))
            factor[used] = np.maximum(C.HABIT_MIN, 1 / (1 + 0.3 * np.log1p(cnt)))
    trips = pd.DataFrame(trips)
    trips["airport_cab"] = airport_cabs(con, trips) & (trips.kind == "car") if len(trips) else []
    tv = pd.DataFrame(rows, columns=["kind", "trip_id", "edge", "t_enter", "secs", "leg_kmh", "stop"])
    loc = pd.to_datetime(tv.t_enter, unit="s", utc=True)
    # local time: use each trip's own UTC offset from its ISO start time
    off = trips.set_index("trip_id").start_time.str.extract(r"([+-]\d\d):?(\d\d)$")
    off_min = (off[0].astype(float) * 60 + np.sign(off[0].astype(float)) * off[1].astype(float)).fillna(330)
    loc = loc + pd.to_timedelta(tv.trip_id.map(off_min).fillna(330).values, unit="m")
    tv["year"], tv["month"], tv["hour"] = loc.dt.year, loc.dt.month, loc.dt.hour + loc.dt.minute / 60
    tv["weekday"], tv["date"] = loc.dt.day_name(), loc.dt.strftime("%Y-%m-%d")
    tv["length_m"] = G.length[tv.edge.values]
    tv["highway"] = [G.highway_names[h] for h in G.highway[tv.edge.values]]
    trips.to_sql("trips", con, index=False, if_exists="replace")
    tv.to_sql("traversals", con, index=False, if_exists="replace")
    con.commit()
    con.close()
    if progress:
        progress(total, total, f"matched {len(trips):,} trips in {time.time() - t0:.0f}s")
    return trips, tv
