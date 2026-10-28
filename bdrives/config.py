"""Settings for Bangalore. To use another city, build a road graph with tools/build_graph.py and change BBOX
and AIRPORT (or pass a different graph to the CLI)."""
from pathlib import Path

# Greater Bangalore incl. NICE Road, the airport and Hoskote: (lat_min, lat_max, lng_min, lng_max)
BBOX = (12.75, 13.27, 77.35, 77.88)
AIRPORT = (13.1986, 77.7066)          # Kempegowda International Airport
GRAPH = Path(__file__).parent / "data" / "bangalore_roads.npz"

CAR_MODES = ("IN_PASSENGER_VEHICLE", "IN_VEHICLE")
BIKE_MODES = ("MOTORCYCLING",)

# trip cleaning
MAX_STEP_KMH = 120        # GPS spike: faster than this between consecutive points
# map matching
SNAP_M = 200              # ignore points further than this from any junction
THIN_M = 300              # thin dense GPS to one point per this many metres
K_CAND = 4                # candidate junctions per point
SNAP_M_PER_S = 5          # snapping 50 m further away costs like 10 s of driving
MAX_DETOUR = 2.5          # routed length vs straight line
MAX_KMH = 100
MAX_LEG_S = 900           # a gap over 15 min between points is a stop, not driving
STOP_KMH = 2              # slower legs are kept on the map but left out of speed stats
HABIT_MIN = 0.5           # 2nd pass: roads used often cost down to half their free-flow time
# airport cab rule
AIRPORT_RADIUS_KM = 3
FLIGHT_WINDOW_H = 6
