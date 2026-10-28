---
name: bangalore-drives
description: Analyse a Google Maps Timeline export (Timeline.json from the phone, or a zip of it) for driving in Bangalore - clean the GPS, snap car and motorbike trips to the road network, and produce speed statistics, a glow map image, an interactive speed map and a time-lapse video. Use when the user wants insights from their Google Maps Timeline / location history, asks about their average driving speed, time stuck in traffic, km per year, which roads they drive, or wants a map or video of their drives. Also adaptable to other cities by building a road graph.
---

# Bangalore drives

Turns a Google Maps Timeline export into road-level driving data for Bangalore, using the bundled
`bdrives` package. All processing runs locally on the user's machine.

## Privacy rules (follow strictly)

- The export is the user's full location history. **Never print raw records, coordinates, place
  addresses or home/work locations** into the conversation. Work from aggregate outputs
  (`report.md`, the tables' counts, speeds, km, years).
- Do not `cat`, `head` or read the Timeline JSON itself; let the scripts parse it.
- If you need to inspect data to debug, print only counts, column names, or values stripped of
  coordinates. Tell the user when you are about to look at anything finer-grained, and why.
- Outputs go to a folder in the user's working directory, never inside the plugin directory.

## Setup (once)

```bash
python3 -m venv "${CLAUDE_PLUGIN_DATA}/venv"
"${CLAUDE_PLUGIN_DATA}/venv/bin/pip" install -q -r "${CLAUDE_PLUGIN_ROOT}/requirements.txt"
```

Use `"${CLAUDE_PLUGIN_DATA}/venv/bin/python"` below (call it `PY`). Run commands with
`PYTHONPATH="${CLAUDE_PLUGIN_ROOT}"` so `python -m bdrives` resolves. Video needs `ffmpeg` on the PATH
(`apt install ffmpeg`, `brew install ffmpeg`, `winget install ffmpeg`); everything else does not.

## Run

1. Find the export. Ask the user for the path if it is not obvious. It is usually `Timeline.json`
   (or a `.zip`) in Downloads. If they have not exported yet, point them to the README section
   "Get your data out of your phone".
2. Process it (single process; ~5-15 min for several years of data, `--fast` halves it):
   ```bash
   PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" $PY -m bdrives run "<path to Timeline.json or .zip>" --out bangalore-drives-output
   ```
   Run it in the background if your harness supports that, and report progress when it finishes.
3. Read `bangalore-drives-output/report.md` and summarise it in plain words: trips, km, hours,
   average speed, time under 10 km/h, speed bands, slowest hours, road types, year-by-year.
   The map image is `bangalore-drives-output/map.png`; offer to open it.
4. Optional: `report`, `image`, `video` subcommands re-run with filters:
   `--no-bikes`, `--keep-airport-cabs`, `--years 2019-2026`; video takes `--size 1080 --seconds 60`.
5. For an interactive UI: `cd "${CLAUDE_PLUGIN_ROOT}" && $PY -m streamlit run app.py`
   (binds to localhost only).

## Answering follow-up questions

The SQLite file `bangalore-drives-output/timeline.db` has:

- `trips`: one row per matched trip: `trip_id, kind (car|bike), mode, start_time, t, t_end, google_m,
  matched_m, legs_ok, legs_bad, airport_cab` (+ start/end coordinates: do not print them)
- `traversals`: one row per road segment driven: `kind, trip_id, edge, t_enter, secs, leg_kmh, stop,
  year, month, hour, weekday, date, length_m, highway`
- `activities`, `visits`, `path_points`, `raw_positions`: the parsed export (contain coordinates)

Compute speeds as `sum(length_m) / sum(secs)` (time-weighted), excluding rows where `stop` is true.
`bdrives.stats.load()` and `bdrives.stats.summary()` already apply the standard filters; prefer them.
Good questions to offer: slowest hour of the week, km per month, how average speed changed by year,
weekday vs weekend, car vs motorbike, days spent under 10 km/h.

## Method (for explaining results)

- Google keeps one track point every ~2-4 minutes for older data (precise fixes only for the last
  ~30 days), so trips are snapped to roads: each point gets its 4 nearest junctions and the cheapest
  drivable sequence wins (Viterbi over shortest-path travel times); a second pass favours roads used
  often. Legs with a detour > 2.5x the straight line, > 100 km/h, or a gap > 15 min are rejected.
- Cleaning: impossible distances capped, "car" legs that are flights relabelled, GPS spikes > 120
  km/h removed. Airport rides on flight days are treated as cabs and excluded by default.
- Speeds are averages per leg (a few hundred metres to a few km), so a single bad junction is spread
  along its stretch.

## Other cities

Build a graph from a Geofabrik extract with `tools/build_graph.py` (see its docstring; needs
`pip install osmium`, ~2-3 GB RAM), then set `BBOX`, `AIRPORT` and `GRAPH` in `bdrives/config.py`.
Copy the plugin to a writable folder first rather than editing the installed plugin.
