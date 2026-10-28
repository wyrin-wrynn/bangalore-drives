"""Read a Google Maps Timeline export (on-device JSON from Android or iPhone, or a .zip containing it) into
pandas tables and a SQLite file:

  visits         a stay at a place: start/end time, location, semantic type
  activities     a movement leg: start/end time and point, distance, mode (IN_PASSENGER_VEHICLE, WALKING, ...)
  path_points    the timestamped track (Google keeps roughly one point every 2-4 minutes)
  raw_positions  precise GPS/Wi-Fi fixes (Google keeps only the last ~30 days)

Basic cleaning happens here: legs with impossible distances are capped, and "car" legs that are really
flights (hundreds of km at aircraft speed) are relabelled FLYING.
"""
import json
import re
import sqlite3
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from .geo import haversine_km

_NUM = r"(-?\d+(?:\.\d+)?)"
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def epoch_s(col):
    """ISO strings with offsets -> seconds since epoch (independent of pandas' datetime resolution)."""
    return (pd.to_datetime(col, utc=True, format="ISO8601") - _EPOCH).dt.total_seconds()


def parse_latlng(v):
    """'12.98°, 77.71°' | 'geo:12.98,77.71' | {'latLng': ...} | {'latitudeE7':..} -> (lat, lng) or (None, None)."""
    if v is None:
        return None, None
    if isinstance(v, dict):
        if "latitudeE7" in v:
            return v["latitudeE7"] / 1e7, v["longitudeE7"] / 1e7
        for k in ("latLng", "LatLng", "point", "placeLocation"):
            if k in v:
                return parse_latlng(v[k])
        return None, None
    m = re.search(_NUM + r"\s*°?\s*,\s*" + _NUM, str(v))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def norm_mode(t):
    """'in passenger vehicle' / 'IN_PASSENGER_VEHICLE' -> 'IN_PASSENGER_VEHICLE'."""
    return re.sub(r"[^A-Z]+", "_", str(t or "UNKNOWN").upper()).strip("_")


def read_json(path):
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".json") and not n.startswith("__MACOSX")]
            if not names:
                raise ValueError("no .json file inside the zip")
            biggest = max(names, key=lambda n: z.getinfo(n).file_size)
            return json.loads(z.read(biggest))
    return json.loads(path.read_text(encoding="utf-8"))


def _segments(data):
    """Normalise both export flavours into an iterable of segment dicts."""
    if isinstance(data, dict) and "semanticSegments" in data:      # Android on-device export
        return data["semanticSegments"], data.get("rawSignals", [])
    if isinstance(data, list):                                      # iPhone export: a plain list of segments
        return data, []
    raise ValueError("unrecognised file: expected a Google Maps Timeline export "
                     "(a dict with 'semanticSegments', or a list of timeline entries)")


def parse(data):
    segs, raw = _segments(data)
    visits, acts, pts, rawp = [], [], [], []
    for i, s in enumerate(segs):
        t0, t1 = s.get("startTime"), s.get("endTime")
        if "visit" in s:
            tc = s["visit"].get("topCandidate", {})
            lat, lng = parse_latlng(tc.get("placeLocation"))
            visits.append({"seg_id": i, "start_time": t0, "end_time": t1, "lat": lat, "lng": lng,
                           "semantic_type": str(tc.get("semanticType", "UNKNOWN")).upper()})
        elif "activity" in s:
            a = s["activity"]
            slat, slng = parse_latlng(a.get("start"))
            elat, elng = parse_latlng(a.get("end"))
            tc = a.get("topCandidate", {})
            acts.append({"seg_id": i, "start_time": t0, "end_time": t1, "start_lat": slat, "start_lng": slng,
                         "end_lat": elat, "end_lng": elng, "distance_m": float(a.get("distanceMeters") or 0),
                         "mode": norm_mode(tc.get("type"))})
        elif "timelinePath" in s:
            base = pd.Timestamp(t0) if t0 else None
            for p in s["timelinePath"]:
                lat, lng = parse_latlng(p.get("point"))
                if p.get("time"):
                    t = p["time"]
                elif base is not None and "durationMinutesOffsetFromStartTime" in p:
                    t = (base + pd.Timedelta(minutes=float(p["durationMinutesOffsetFromStartTime"]))).isoformat()
                else:
                    continue
                pts.append({"time": t, "lat": lat, "lng": lng})
    for r in raw:
        p = r.get("position")
        if p:
            lat, lng = parse_latlng(p.get("LatLng") or p.get("latLng"))
            rawp.append({"time": p.get("timestamp"), "lat": lat, "lng": lng,
                         "accuracy_m": p.get("accuracyMeters"), "source": p.get("source")})
    out = {"visits": pd.DataFrame(visits), "activities": pd.DataFrame(acts),
           "path_points": pd.DataFrame(pts, columns=["time", "lat", "lng"]),
           "raw_positions": pd.DataFrame(rawp, columns=["time", "lat", "lng", "accuracy_m", "source"])}
    for name, df in out.items():
        if len(df):
            tcol = "start_time" if "start_time" in df else "time"
            df["t"] = epoch_s(df[tcol])
            local = pd.to_datetime(df[tcol].str.slice(0, 19), format="%Y-%m-%dT%H:%M:%S", errors="coerce")
            df["date"], df["year"] = local.dt.strftime("%Y-%m-%d"), local.dt.year
            df["hour"] = local.dt.hour + local.dt.minute / 60
            df["weekday"] = local.dt.day_name()
            df.dropna(subset=[c for c in ("lat", "start_lat") if c in df], inplace=True)
    a = out["activities"]
    if len(a):
        a["t_end"] = epoch_s(a.end_time)
        fix_activities(a)
    return out


def fix_activities(a):
    """Relabel flights logged as ground travel; cap distances implying impossible speeds."""
    a["distance_m_raw"], a["mode_raw"] = a.distance_m, a["mode"]
    straight = haversine_km(a.start_lat, a.start_lng, a.end_lat, a.end_lng) * 1000
    hours = ((a.t_end - a.t) / 3600).clip(lower=1 / 60)
    ground = a["mode"] != "FLYING"
    flight = ground & (straight / 1000 / hours > 200) & (straight > 300_000)
    a.loc[flight, "mode"] = "FLYING"
    a.loc[flight, "distance_m"] = straight[flight]
    glitch = ground & ~flight & (a.distance_m / 1000 / hours > 150)
    a.loc[glitch, "distance_m"] = straight[glitch] * 1.3
    a["fixed"] = np.select([flight, glitch], ["relabelled_flight", "distance_capped"], "")


def load(path, db):
    tables = parse(read_json(path))
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    for name, df in tables.items():
        df.to_sql(name, con, index=False, if_exists="replace")
    con.commit()
    con.close()
    return {k: len(v) for k, v in tables.items()}
