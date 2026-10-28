"""Bangalore drives: upload your Google Maps Timeline export and explore it. Everything runs on this machine.

    streamlit run app.py
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

from bdrives import config as C
from bdrives import match, render, stats
from bdrives.load import load

OUT_ROOT = Path("output")
st.set_page_config(page_title="Bangalore drives", page_icon="🚗", layout="wide")


@st.cache_resource
def graph():
    return match.Graph()


@st.cache_data(show_spinner=False)
def filtered(db, kinds, no_cabs, years):
    return stats.load(db, kinds, no_cabs, years)


@st.cache_data(show_spinner=False)
def summary(db, kinds, no_cabs, years):
    return stats.summary(*filtered(db, kinds, no_cabs, years))


def runs():
    return sorted([p for p in OUT_ROOT.glob("*/timeline.db") if _matched(p)], key=lambda p: p.stat().st_mtime, reverse=True)


def _matched(db):
    import sqlite3
    try:
        return sqlite3.connect(db).execute("SELECT count(*) FROM trips").fetchone()[0] > 0
    except Exception:
        return False


def process(upload, fast):
    data = upload.getvalue()
    run_dir = OUT_ROOT / hashlib.sha1(data).hexdigest()[:12]
    db = run_dir / "timeline.db"
    if _matched(db):
        return db
    run_dir.mkdir(parents=True, exist_ok=True)
    src = run_dir / ("upload.zip" if upload.name.lower().endswith(".zip") else "upload.json")
    src.write_bytes(data)
    with st.status("Processing your export (this stays on your computer)", expanded=True) as status:
        st.write("Reading the file ...")
        counts = load(src, db)
        st.write(", ".join(f"{v:,} {k.replace('_', ' ')}" for k, v in counts.items()))
        st.write("Snapping trips to Bangalore's roads. This is the slow part: a few minutes for a few years of data.")
        bar = st.progress(0.0)
        match.run(db, bikes=True, passes=1 if fast else 2, graph=C.GRAPH,
                  progress=lambda d, t, m: bar.progress(min(d / max(t, 1), 1.0), text=m))
        status.update(label="Done", state="complete")
    src.unlink()          # keep only the processed tables
    return db


# ---------------------------------------------------------------- sidebar: data source + filters
st.sidebar.title("🚗 Bangalore drives")
previous = runs()
db = None
if previous:
    labels = {str(p): f"{p.parent.name}  ({pd.Timestamp(p.stat().st_mtime, unit='s'):%d %b %H:%M})" for p in previous}
    pick = st.sidebar.selectbox("Processed exports", ["(upload a new one)"] + list(labels), format_func=lambda k: labels.get(k, k))
    if pick != "(upload a new one)":
        db = pick

if db is None:
    st.title("Your drives in Bangalore, from your Google Maps Timeline")
    st.markdown("Upload the **Timeline.json** you exported from your phone (or a **.zip** of it). "
                "It is processed on this computer only; nothing is uploaded anywhere. "
                "See the README for how to export it on Android or iPhone.")
    upload = st.file_uploader("Timeline export", type=["json", "zip"])
    fast = st.checkbox("Fast mode (one matching pass, about 2x quicker, slightly less accurate route choice)")
    if upload and st.button("Process", type="primary"):
        db = str(process(upload, fast))
        st.rerun()
    st.stop()

st.sidebar.markdown("**Filters**")
bikes = st.sidebar.checkbox("Include motorbike trips", True)
no_cabs = st.sidebar.checkbox("Leave out airport rides on flight days (cabs)", True)
trips_all, tv_all = filtered(db, ("car", "bike"), False, None)
y0, y1 = int(tv_all.year.min()), int(tv_all.year.max())
years = st.sidebar.slider("Years", y0, y1, (y0, y1)) if y1 > y0 else (y0, y1)
kinds = ("car", "bike") if bikes else ("car",)
trips, tv = filtered(db, kinds, no_cabs, years)
if trips.empty:
    st.warning("No trips for these filters.")
    st.stop()
s = summary(db, kinds, no_cabs, years)
st.sidebar.caption(f"{s['trips']:,} trips · {s['km']:,.0f} km · {s['first']} to {s['last']}")

st.title("Your drives in Bangalore")
tabs = st.tabs(["Overview", "Year by year", "Map", "Speed map", "Video"])

# ---------------------------------------------------------------- overview
with tabs[0]:
    k = st.columns(5)
    k[0].metric("Trips", f"{s['trips']:,}")
    k[1].metric("Distance", f"{s['km']:,.0f} km")
    k[2].metric("Time driving", f"{s['hours']:,.0f} h")
    k[3].metric("Average speed", f"{s['avg_kmh']:.1f} km/h")
    k[4].metric("Under 10 km/h", f"{s['slow_hours'] / 24:.1f} days")
    st.caption(f"Median trip: {s['trip_km'][0.5]:g} km, {s['trip_min'][0.5]:g} min, {s['trip_kmh'][0.5]:g} km/h. "
               f"One trip in ten averages under {s['trip_kmh'][0.1]:g} km/h.")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Speed bands (km/h)")
        st.bar_chart(s["bands"], stack=False, height=280)
        st.dataframe(s["bands"], width="stretch")
    with c2:
        st.subheader("Weekday average speed by hour")
        st.line_chart(s["by_hour"], height=280)
        st.subheader("By road type")
        st.dataframe(s["by_road"], width="stretch")
    if len(s["by_kind"]) > 1:
        st.subheader("Car vs motorbike")
        st.dataframe(s["by_kind"], width="stretch")

# ---------------------------------------------------------------- years
with tabs[1]:
    c1, c2 = st.columns(2)
    c1.subheader("Km per year")
    c1.bar_chart(s["by_year"]["km"], height=300)
    c2.subheader("Average speed per year (km/h)")
    c2.line_chart(s["by_year"]["avg km/h"], height=300)
    st.subheader("Km per month")
    st.bar_chart(s["by_month"], height=260)
    st.dataframe(s["by_year"], width="stretch")

# ---------------------------------------------------------------- glow map image
with tabs[2]:
    run_dir = Path(db).parent
    tag = f"{'cb' if bikes else 'c'}{'n' if no_cabs else 'a'}{years[0]}-{years[1]}"
    img = run_dir / f"map_{tag}.png"
    if not img.exists():
        with st.spinner("Drawing the map (about 20 s) ..."):
            render.image(graph(), trips, tv, img)
    st.image(str(img), width="stretch")
    st.download_button("Download PNG", img.read_bytes(), file_name="bangalore_drives.png", mime="image/png")

# ---------------------------------------------------------------- interactive speed map
with tabs[3]:
    band = st.radio("Time of day", ["All day", "Morning 8-10", "Midday 11-15", "Evening 17-20", "Night 21-6"], horizontal=True)
    rng = {"All day": (0, 24), "Morning 8-10": (8, 10), "Midday 11-15": (11, 15), "Evening 17-20": (17, 20),
           "Night 21-6": (21, 6)}[band]
    weekdays_only = st.checkbox("Weekdays only", True)
    min_trips = st.slider("Hide roads with fewer trips than", 1, 20, 3)
    x = tv[~tv.stop.astype(bool)]
    if weekdays_only:
        x = x[~x.weekday.isin(["Saturday", "Sunday"])]
    x = x[(x.hour >= rng[0]) & (x.hour < rng[1])] if rng[0] < rng[1] else x[(x.hour >= rng[0]) | (x.hour < rng[1])]
    per = x.groupby(["edge", "trip_id"]).leg_kmh.mean().groupby(level=0).agg(["median", "size"])
    per = per[per["size"] >= min_trips]
    if per.empty:
        st.info("No roads with enough trips for this selection.")
    else:
        G = graph()
        stops = np.array([8, 14, 20, 28, 38])
        cols = np.array([[200, 30, 30], [235, 120, 30], [240, 200, 40], [120, 200, 80], [40, 170, 90]])
        kk = np.clip(np.interp(per["median"].values, stops, np.arange(5)), 0, 4)
        lo = np.floor(kk).astype(int).clip(0, 3)
        rgb = (cols[lo] * (1 - (kk - lo))[:, None] + cols[lo + 1] * (kk - lo)[:, None]).astype(int)
        data = pd.DataFrame({
            "path": [G.edge_coords(e).round(5).tolist() for e in per.index],
            "color": [c.tolist() + [230] for c in rgb],          # plain ints: numpy ints render black
            "width": (8 + 10 * np.log1p(per["size"].values)).round(1),
            "tip": [f"<b>{G.names[G.name_id[e]] or '(unnamed road)'}</b><br/>{m:.0f} km/h median · {n} trips"
                    for e, m, n in zip(per.index, per["median"], per["size"])]})
        # sizes in metres with pixel clamps: pixel-unit sizes can flood the canvas in some deck.gl builds
        st.pydeck_chart(pdk.Deck(
            layers=[pdk.Layer("PathLayer", data, get_path="path", get_color="color", get_width="width",
                              width_min_pixels=1.5, width_max_pixels=8, pickable=True, auto_highlight=True)],
            initial_view_state=pdk.ViewState(latitude=12.96, longitude=77.62, zoom=11.2),
            map_provider="carto", map_style=pdk.map_styles.CARTO_DARK,
            tooltip={"html": "{tip}", "style": {"fontSize": "12px"}}), height=650)
        st.caption("Colour = your median speed on that road: red under 10 km/h, amber ~20, green 28+. "
                   "Thicker = more trips. Base map tiles come from CARTO (only the map area is requested).")

# ---------------------------------------------------------------- video
with tabs[4]:
    st.markdown("A time-lapse of every trip in order: each road glows brighter the more often it was driven. "
                "Needs **ffmpeg** installed. Takes a few minutes depending on your machine.")
    preset = st.radio("Made for", list(render.PRESETS), horizontal=True,
                      format_func={"hq": "Keeping (HQ)", "reddit": "Reddit", "whatsapp": "WhatsApp status"}.get)
    st.caption(render.PRESETS[preset]["desc"] + ". Platforms re-compress uploads, so the Reddit and WhatsApp versions "
               "use thicker lines and bigger text to stay sharp.")
    area = st.radio("Area", ["full", "core"], horizontal=True, index=["full", "core"].index(render.PRESETS[preset]["area"]),
                    format_func={"full": "Greater Bangalore", "core": "City core (zoomed in)"}.get)
    vid = Path(db).parent / f"drives_{tag}_{preset}_{area}.mp4"
    if vid.exists():
        st.video(str(vid))
        st.download_button("Download MP4", vid.read_bytes(), file_name="bangalore_drives.mp4", mime="video/mp4")
    elif st.button("Render video", type="primary"):
        bar = st.progress(0.0)
        try:
            render.video(graph(), tv, vid, preset=preset, area=area,
                         progress=lambda d, t, m: bar.progress(min(d / max(t, 1), 1.0), text=m))
            st.rerun()
        except RuntimeError as e:
            st.error(str(e))
