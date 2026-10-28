# bangalore-drives

Your Google Maps Timeline, snapped to Bangalore's roads.

Export your location history from your phone, drop the file into a local app, and get:

- a glow map of every car and motorbike trip you have taken in Bangalore, brighter where you drive more
- your average driving speed, time spent under 10 km/h, speed bands, slowest hours, speed by road type
- km and speed year by year and month by month
- an interactive speed map: every road you have driven, coloured by your typical speed at that time of day
- a time-lapse video of all your trips, replayed in order

**Everything runs on your computer.** The file is never uploaded anywhere. The only network requests
are the background map tiles in the interactive speed map (from CARTO, they see the map area, not
your data).

---

## 1. Get your data out of your phone

Google stopped keeping Timeline in the cloud in 2024. It now lives on your phone, and you export it
from there as a single `.json` file (usually 10-100 MB).

**Android**

1. Open the phone's **Settings** app.
2. Go to **Location** → **Location services** → **Timeline**.
3. Tap **Export Timeline data** and save the file (for example to Downloads).

**iPhone**

1. Open **Google Maps** and tap your profile picture.
2. Tap **Your Timeline**, then the **⋯** menu.
3. Choose **Location & privacy settings** → **Export Timeline data**, and save to Files.

Menu names move around between app versions. If yours differ, look for "Location & privacy" under
your profile or Maps settings.

The file is usually called `Timeline.json` (it may have another name on non-English phones). Any name
works. If Timeline was never turned on, there will be nothing to export.

## 2. Move the file to your laptop

The file is your full location history, so treat it like a passport scan. A few ways to move it, most
private first:

| How | Steps |
|---|---|
| **USB cable** | Android: plug in, pick "File transfer", copy from Downloads. iPhone: Finder (Mac) or Apple Devices / iTunes (Windows) → Files. |
| **AirDrop** (iPhone → Mac) | Files app → long-press the file → Share → AirDrop. |
| **Quick Share / Nearby Share** (Android → Windows / Chromebook) | Files app → Share → Quick Share. |
| **WhatsApp "Message yourself"** | Share the file to your own chat **as a Document** (not as a photo), then download it from WhatsApp Web or Desktop. |
| **Email to yourself** | **Zip it first**: JSON shrinks about 10x, so it usually fits under the 25 MB attachment limit. Bigger files become a Drive link. |
| **Google Drive / OneDrive / Telegram Saved Messages** | Upload from the phone, download on the laptop. |

The app accepts the `.zip` directly, so there is no need to unzip. When you are done, delete the copies
you sent through chat, email or cloud storage.

## 3. Install

You need Python 3.10 or newer and git. For the video you also need ffmpeg (optional).

```bash
git clone https://github.com/wyrin-wrynn/bangalore-drives.git
cd bangalore-drives
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

ffmpeg, only for the video: `sudo apt install ffmpeg` (Ubuntu), `brew install ffmpeg` (Mac),
`winget install ffmpeg` (Windows).

## 4. Run the app

```bash
streamlit run app.py
```

Your browser opens on `http://localhost:8501`. Upload the `.json` or `.zip` and press **Process**.

- Snapping trips to roads is the slow part: a few minutes for a year or two of history, about 20 minutes
  for 10+ years (7,000 trips) on a laptop, using about 1-2 GB of RAM. Tick **Fast mode** to halve it.
- When it finishes, the results open by themselves. Next time they are listed in the sidebar, so there is no
  need to upload again.
- Results are saved under `output/` (the uploaded file itself is deleted after processing).
- Sidebar filters: include motorbike trips, leave out airport rides on flight days, year range.
- Tabs: **Overview**, **Year by year**, **Map** (downloadable PNG), **Speed map**, **Video** (render, then
  pick Reddit / WhatsApp / HQ, render, then download the MP4).

## 5. Or use the command line

```bash
python -m bdrives run ~/Downloads/Timeline.json      # process + report + map image into output/
python -m bdrives report --years 2019-2026           # reprint stats with filters
python -m bdrives image --no-bikes                   # redraw the glow map
python -m bdrives video --preset reddit             # time-lapse (needs ffmpeg)
```

`output/report.md` has the full stats, `output/map.png` the map, `output/drives_<preset>.mp4` the video.

### Video presets: made for where you will post it

Reddit, WhatsApp and Instagram re-compress every upload. Thin 1-2 px lines and small text turn fuzzy, so
each preset scales the line width and text to its output size and picks a bitrate the platform keeps.

| Preset | Size | Length | Best for |
|---|---|---|---|
| `hq` (default) | 1920 x 1920 | 60 s | keeping, presentations, big screens |
| `reddit` | 1080 x 1080, thicker lines, bigger text | 45 s | Reddit, X, LinkedIn (they serve 1080p at most) |
| `whatsapp` | 720 x 1280 vertical, city core, under 16 MB | 30 s | WhatsApp status, Instagram stories |

Add `--area core` to zoom into central Bangalore (roads look bigger) or `--area full` for all of Greater
Bangalore. `--seconds 20` makes it shorter. Upload the MP4 itself, not a GIF: GIFs are larger and blurrier.

## 6. Or ask Claude Code to do it

This repo is also a [Claude Code](https://code.claude.com) plugin with a skill that runs the whole
pipeline for you and answers follow-up questions ("what is my slowest hour?", "how did 2022 compare?").

```bash
claude plugin marketplace add wyrin-wrynn/bangalore-drives
claude plugin install bangalore-drives@bangalore-drives
```

Then, in Claude Code: *"Analyse my Google Maps Timeline export in ~/Downloads/Timeline.json"*.

Privacy note: the heavy processing still runs locally, but Claude reads the summaries it produces (and
anything it inspects while helping), and that text goes to the model provider like any conversation.
The skill tells Claude not to print coordinates or raw records. If you want nothing to leave your
machine, use the app or the command line instead.

---

## How it works

1. **Read the export.** The Android format (`semanticSegments`) and the iPhone format (a list of
   entries with `geo:` coordinates) are both parsed into visits, movement legs (with a mode such as car,
   motorbike, walking, flying) and the timestamped track.
2. **Clean it.** Legs with impossible distances are capped (exports contain things like 8,000 km in 4
   minutes). "Car" legs that are really flights are relabelled. GPS jumps faster than 120 km/h between
   points are removed.
3. **Snap trips to roads.** Google keeps one track point every 2-4 minutes for older data, which is
   500 m to 2 km apart in city traffic. Straight lines between them cut through lakes and buildings.
   So every car and motorbike trip inside Greater Bangalore is matched to the road network: each point
   gets its four nearest junctions as candidates, and the sequence that is cheapest to drive over the
   whole trip wins. A second pass makes roads you use often slightly cheaper, so close calls go to your
   usual route. Legs needing a detour over 2.5x the straight line, over 100 km/h, or a gap over 15
   minutes are rejected. Each leg's time is spread over its roads, which gives a speed for every road
   at every hour.
4. **Airport cabs.** Google cannot tell your car from a cab. A ride to the airport with a flight a few
   hours later (or from the airport just after landing) is treated as a cab and left out by default.
   Same-day drop-offs and pick-ups stay in.
5. **Stats and pictures.** Speeds are time-weighted (total km / total hours). Maps and video are drawn
   with NumPy and Pillow; the video frames are streamed into ffmpeg (GPU encoding when available).

The road network (`bdrives/data/bangalore_roads.npz`, 16 MB) is prebuilt from OpenStreetMap for
Greater Bangalore (12.75-13.27 N, 77.35-77.88 E, including NICE Road, the airport and Hoskote): about
3.2 lakh junctions and 7.6 lakh one-way road segments. Rebuild it with `tools/build_graph.py`.

## What is included and what is not

- Included: car trips (`IN_PASSENGER_VEHICLE`, `IN_VEHICLE`) and motorbike trips that start and end
  inside Greater Bangalore.
- Left out: trips that leave the city (road trips, travel elsewhere), flights, walking, cycling, and by
  default airport cabs on flight days.
- Days your phone did not log are simply missing, so totals can be lower than your odometer.

## Limitations

- Speeds are averages over each gap between points (a few hundred metres to a few km in older data), so
  one bad junction is spread along its stretch.
- Older years have fewer points per trip, so their matched routes are less certain.
- iPhone exports are supported from the documented format but have been tested less than Android ones.
  If yours fails to load, please open an issue with the error message (not the file).
- Other cities: build a graph for your city with `tools/build_graph.py` and change `BBOX`, `AIRPORT` and
  `GRAPH` in `bdrives/config.py`.

## Credits and license

- Code: MIT (see `LICENSE`).
- Road data: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, ODbL, via
  [Geofabrik](https://download.geofabrik.de/).
- Speed map base tiles: © [CARTO](https://carto.com/attributions).
- Not affiliated with Google. "Google Maps" and "Timeline" are Google's names for their product.

---

Made by the person behind [India, Analyzed](https://www.indiaanalyzed.com) ·
[Substack](https://indiaanalyzed.substack.com)
