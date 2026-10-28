"""Driving statistics from the matched tables (`trips`, `traversals`), with simple filters."""
import sqlite3

import numpy as np
import pandas as pd

ROAD_CLASS = {"motorway": "highway / expressway", "trunk": "major arterial", "primary": "primary road",
              "secondary": "secondary road", "tertiary": "connecting road", "unclassified": "local / residential",
              "residential": "local / residential", "living_street": "local / residential", "road": "local / residential"}
BANDS = [0, 10, 20, 30, 40, 60, 1000]
BAND_LABELS = ["under 10", "10-20", "20-30", "30-40", "40-60", "over 60"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def load(db, kinds=("car", "bike"), exclude_airport_cabs=True, years=None):
    con = sqlite3.connect(db)
    trips = pd.read_sql("SELECT * FROM trips", con)
    tv = pd.read_sql("SELECT * FROM traversals", con)
    con.close()
    trips = trips[trips.kind.isin(kinds) & (trips.matched_m > 0)]
    if exclude_airport_cabs:
        trips = trips[~trips.airport_cab.astype(bool)]
    tv = tv[tv.trip_id.isin(trips.trip_id)]
    if years:
        tv = tv[tv.year.between(*years)]
        trips = trips[trips.trip_id.isin(tv.trip_id)]
    return trips, tv


def kmh(df):
    return df.length_m.sum() / 1000 / (df.secs.sum() / 3600) if df.secs.sum() else np.nan


def summary(trips, tv):
    mv = tv[~tv.stop.astype(bool)]
    per_trip = mv.groupby("trip_id").agg(km=("length_m", lambda s: s.sum() / 1000), h=("secs", lambda s: s.sum() / 3600))
    per_trip["kmh"] = per_trip.km / per_trip.h
    band = pd.cut(mv.leg_kmh, BANDS, right=False, labels=BAND_LABELS)
    bands = pd.DataFrame({
        "share of driving time %": mv.groupby(band, observed=False).secs.sum() / mv.secs.sum() * 100,
        "share of distance %": mv.groupby(band, observed=False).length_m.sum() / mv.length_m.sum() * 100}).round(1)
    bands.index.name = "speed (km/h)"
    wd = mv[~mv.weekday.isin(["Saturday", "Sunday"])]
    by_hour = wd.groupby(wd.hour.astype(int)).apply(kmh, include_groups=False).rename("km/h (weekdays)")
    # skip thin hours; the bar scales with history size (3 trips for a month, 20 for years of data)
    min_trips = int(np.clip(per_trip.shape[0] // 300, 3, 20))
    by_hour = by_hour[wd.groupby(wd.hour.astype(int)).trip_id.nunique() >= min_trips]
    roads = mv.assign(road=mv.highway.str.replace("_link", "").map(ROAD_CLASS).fillna("other"))
    by_road = roads.groupby("road").apply(lambda g: pd.Series({
        "km": g.length_m.sum() / 1000, "avg km/h": kmh(g), "share of km %": g.length_m.sum() / mv.length_m.sum() * 100}),
        include_groups=False).sort_values("km", ascending=False).round(1)
    by_year = mv.groupby("year").apply(lambda g: pd.Series({
        "trips": g.trip_id.nunique(), "km": g.length_m.sum() / 1000, "hours": g.secs.sum() / 3600, "avg km/h": kmh(g)}),
        include_groups=False).round(1)
    by_year.index = by_year.index.astype(str)          # plain years on chart axes, not 2,013
    by_month = mv.assign(ym=mv.date.str.slice(0, 7)).groupby("ym").length_m.sum().div(1000).round(0).rename("km")
    by_weekday = mv.groupby("weekday").apply(kmh, include_groups=False).reindex(WEEKDAYS).rename("km/h")
    by_kind = mv.groupby("kind").apply(lambda g: pd.Series({
        "trips": g.trip_id.nunique(), "km": g.length_m.sum() / 1000, "avg km/h": kmh(g)}), include_groups=False).round(1)
    return {
        "trips": int(per_trip.shape[0]), "km": float(mv.length_m.sum() / 1000), "hours": float(mv.secs.sum() / 3600),
        "avg_kmh": float(kmh(mv)), "roads": int(tv.edge.nunique()),
        "first": str(tv.date.min()), "last": str(tv.date.max()),
        "trip_km": per_trip.km.quantile([.25, .5, .75, .9]).round(1),
        "trip_min": (per_trip.h * 60).quantile([.25, .5, .75, .9]).round(0),
        "trip_kmh": per_trip.kmh.quantile([.1, .25, .5, .75, .9]).round(1),
        "bands": bands, "by_hour": by_hour.round(1), "by_road": by_road, "by_year": by_year, "by_month": by_month,
        "by_weekday": by_weekday.round(1), "by_kind": by_kind,
        "slow_hours": float(mv[mv.leg_kmh < 10].secs.sum() / 3600),
    }


def report_md(s, options=""):
    q = lambda x: ", ".join(f"p{int(k * 100)} {v:g}" for k, v in x.items())
    lines = [f"# Driving report", "", f"_{options}_" if options else "", "",
             f"- {s['trips']:,} trips, {s['km']:,.0f} km in {s['hours']:,.0f} hours of driving, {s['first']} to {s['last']}",
             f"- Average speed: **{s['avg_kmh']:.1f} km/h**",
             f"- Time spent under 10 km/h: {s['slow_hours']:,.0f} hours ({s['slow_hours'] / 24:.1f} days)",
             f"- Distinct road segments driven: {s['roads']:,}",
             f"- Trip length (km): {q(s['trip_km'])}", f"- Trip duration (min): {q(s['trip_min'])}",
             f"- Trip average speed (km/h): {q(s['trip_kmh'])}", "",
             "## Speed bands (km/h)", "", s["bands"].to_markdown(), "",
             "## By road type", "", s["by_road"].to_markdown(), "",
             "## By year", "", s["by_year"].to_markdown(), "",
             "## Weekday average speed by hour", "", s["by_hour"].to_frame().to_markdown(), "",
             "## By day of week", "", s["by_weekday"].to_frame().to_markdown(), ""]
    if len(s["by_kind"]) > 1:
        lines += ["## Car vs bike", "", s["by_kind"].to_markdown(), ""]
    return "\n".join(lines)
