"""Glow map image and time-lapse video of matched trips.

Each road segment glows brighter the more trips used it (log scale). Cars are orange, motorbikes teal.
The video replays traversals in time order: slow at first (single trips road by road), then accelerating
(frame f shows traversals up to N * (f/F)^PACE). Frames are drawn with NumPy + Pillow and piped into ffmpeg;
the timeline is split across worker processes where the OS allows it.
"""
import multiprocessing as mp
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import gaussian_filter, zoom

from . import config as C

BG = np.array([10, 12, 18], np.float32)
ORANGE = np.array([[60, 10, 0], [190, 60, 5], [255, 140, 20], [255, 205, 90], [255, 245, 220]], np.float32)
TEAL = np.array([[0, 30, 40], [0, 110, 120], [20, 190, 200], [120, 235, 240], [225, 255, 255]], np.float32)
RECENT_RGB = np.array([108, 207, 230], np.float32)
PACE, HOLD_S, RECENT_DECAY, RECENT_REPLAY, GLOW_DOWN = 2.2, 4, 0.86, 30, 4
FONTS = [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
         ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
         ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
         ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf")]


def font(size, bold=False):
    for regular, heavy in FONTS:
        path = heavy if bold else regular
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)     # Pillow's built-in scalable font


def ramp(v, stops):
    v = np.clip(v, 0, 1) * (len(stops) - 1)
    lo = np.floor(v).astype(int).clip(0, len(stops) - 2)
    f = (v - lo)[..., None]
    return stops[lo] * (1 - f) + stops[lo + 1] * f


class View:
    """lat/lng box -> W x H pixels (equirectangular, cos-lat corrected); height follows the box shape."""

    def __init__(self, bbox, width, pad=0.02):
        lat0, lat1, lng0, lng1 = bbox
        self.k = np.cos(np.radians((lat0 + lat1) / 2))
        w_deg, h_deg = (lng1 - lng0) * self.k, lat1 - lat0
        self.W = int(width) - int(width) % GLOW_DOWN
        self.scale = self.W / (w_deg * (1 + 2 * pad))
        self.H = int(round(h_deg * (1 + 2 * pad) * self.scale))
        self.H -= self.H % GLOW_DOWN
        self.lng0, self.lat1 = lng0 - w_deg * pad / self.k, lat1 + h_deg * pad

    def px(self, lng, lat):
        return (np.asarray(lng) - self.lng0) * self.k * self.scale, (self.lat1 - np.asarray(lat)) * self.scale


def raster(x, y, W, H, step=0.6):
    """Flat pixel indices along polylines (NaN separates lines)."""
    x0, y0, x1, y1 = x[:-1], y[:-1], x[1:], y[1:]
    ok = np.isfinite(x0) & np.isfinite(x1)
    x0, y0, x1, y1 = x0[ok], y0[ok], x1[ok], y1[ok]
    n = np.maximum(1, np.ceil(np.hypot(x1 - x0, y1 - y0) / step)).astype(int) + 1
    seg = np.repeat(np.arange(len(n)), n)
    f = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n - 1, n).clip(1)
    X = np.round(x0[seg] + f * (x1[seg] - x0[seg])).astype(int)
    Y = np.round(y0[seg] + f * (y1[seg] - y0[seg])).astype(int)
    inside = (X >= 0) & (X < W) & (Y >= 0) & (Y < H)
    return np.unique(Y[inside] * W + X[inside])


def background(G, view):
    """Faint road network; major roads brighter."""
    levels = {"motorway": 46, "trunk": 46, "primary": 40, "secondary": 32, "tertiary": 26}
    lv_of = np.array([levels.get(h.replace("_link", ""), 16) for h in G.highway_names])[G.highway]
    first = G.geo[G.geo_off[:-1]]
    lat0, lat1, lng0, lng1 = C.BBOX
    inview = np.nonzero((first[:, 1] > lat0 - .05) & (first[:, 1] < lat1 + .05) &
                        (first[:, 0] > lng0 - .05) & (first[:, 0] < lng1 + .05))[0]
    bg = np.zeros(view.W * view.H, np.float32)
    for lv in np.unique(lv_of[inview]):
        es = inview[lv_of[inview] == lv]
        for c in range(0, len(es), 40000):
            xy = np.vstack([np.vstack([G.edge_coords(e), [[np.nan, np.nan]]]) for e in es[c:c + 40000]])
            idx = raster(*view.px(xy[:, 0], xy[:, 1]), view.W, view.H, step=0.8)
            bg[idx] = np.maximum(bg[idx], lv)
    return (BG + bg.reshape(view.H, view.W, 1) * np.array([0.75, 0.85, 1.0], np.float32)).astype(np.float32)


def edge_pixels(G, edges, view, brush):
    out, mid = {}, {}
    for e in edges:
        xy = G.edge_coords(e)
        x, y = view.px(xy[:, 0], xy[:, 1])
        base = raster(x, y, view.W, view.H)
        out[e] = np.unique(np.concatenate([base + dy * view.W + dx for dy in range(brush) for dx in range(brush)])
                           .clip(0, view.W * view.H - 1))
        mid[e] = (float(x[len(x) // 2]), float(y[len(y) // 2]))
    return out, mid


def glow(inten, view):
    h, w = view.H // GLOW_DOWN, view.W // GLOW_DOWN
    lo = inten.reshape(h, GLOW_DOWN, w, GLOW_DOWN).mean(axis=(1, 3))
    return zoom(gaussian_filter(lo, sigma=max(view.W, view.H) / 360 / GLOW_DOWN * 1.6), GLOW_DOWN, order=1)


def layer(counts, pix, view, stops, ref=None):
    acc = np.zeros(view.W * view.H, np.float32)
    if len(counts):
        idx = np.concatenate([pix[e] for e in counts.index])
        np.add.at(acc, idx, np.repeat(counts.values.astype(np.float32), [len(pix[e]) for e in counts.index]))
    ref = ref or (np.log1p(np.quantile(acc[acc > 0], 0.995)) if (acc > 0).any() else 1.0)
    inten = np.where(acc > 0, 0.3 + 0.7 * np.clip(np.log1p(acc) / ref, 0, 1), 0).reshape(view.H, view.W)
    g = glow(inten, view)
    return ramp(inten, stops) * inten[..., None] + ramp(g, stops) * (0.55 * g)[..., None]


def image(G, trips, tv, out, width=3000, title="Every drive in Bangalore"):
    view = View(C.BBOX, width)
    rgb = background(G, view)
    counts = {k: tv[tv.kind == k][["trip_id", "edge"]].drop_duplicates().edge.value_counts() for k in ("car", "bike")}
    pix, _ = edge_pixels(G, set(tv.edge.unique()), view, max(2, round(width / 700)))
    rgb = rgb + layer(counts["car"], pix, view, ORANGE) + 0.9 * layer(counts["bike"], pix, view, TEAL)
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    pad, s = view.W // 40, view.W // 60
    fb, fm = font(view.W // 34, True), font(view.W // 62)
    mv = tv[~tv.stop.astype(bool)]              # same basis as the stats: moving legs only
    d.text((pad, pad), title, font=fb, fill=(240, 240, 245))
    d.text((pad, pad + view.W // 26), f"{tv.date.min()[:4]} to {tv.date.max()[:4]}   ·   {mv.trip_id.nunique():,} trips   ·   "
           f"{mv.length_m.sum() / 1000:,.0f} km on {tv.edge.nunique():,} road segments", font=fm, fill=(170, 175, 190))
    y = view.H - pad - 2.4 * s
    for kind, colour in (("car", (255, 160, 40)), ("bike", (40, 200, 210))):
        k = mv[mv.kind == kind]
        if len(k):
            d.rounded_rectangle([pad, y + s * .25, pad + s * 1.6, y + s * .55], radius=s * .15, fill=colour)
            d.text((pad + s * 2, y), f"{'car' if kind == 'car' else 'motorbike'}  {k.trip_id.nunique():,} trips, "
                   f"{k.length_m.sum() / 1000:,.0f} km", font=fm, fill=(225, 225, 232))
            y += 1.3 * s
    d.text((view.W - pad, view.H - pad - 1.1 * s), "brighter = driven more often   ·   map data © OpenStreetMap",
           font=fm, fill=(130, 135, 150), anchor="ra")
    out = Path(out)
    img.save(out, optimize=True)
    return out


# ---------------------------------------------------------------- video
# Platforms re-encode uploads: thin 1-2 px lines and small text turn to mush. Presets scale line width and
# text with the output size and set bitrate so the result survives the platform's compression.
PRESETS = {
    "hq": dict(width=1920, height=None, fps=24, seconds=60, line=1 / 640, text=1.0, area="full",
               x264=["-crf", "18"], nvenc=["-cq", "19"], desc="1920 px square, full detail, for keeping"),
    "reddit": dict(width=1080, height=None, fps=30, seconds=45, line=1 / 330, text=1.3, area="full",
                   x264=["-crf", "17", "-maxrate", "12M", "-bufsize", "24M"], nvenc=["-cq", "18", "-maxrate", "12M", "-bufsize", "24M"],
                   desc="1080 px square, thicker lines and bigger text, high bitrate (Reddit re-encodes to 1080p)"),
    "whatsapp": dict(width=720, height=1280, fps=30, seconds=30, line=1 / 230, text=1.45, area="core",
                     x264=["-b:v", "3400k", "-maxrate", "3800k", "-bufsize", "7600k"],
                     nvenc=["-rc", "vbr", "-b:v", "3400k", "-maxrate", "3800k", "-bufsize", "7600k"],
                     desc="720x1280 vertical for Status, city core, under WhatsApp's 16 MB limit"),
}
CORE_BBOX = (12.84, 13.10, 77.49, 77.80)     # central Bangalore incl. the ORR ring
_S = {}   # shared, read-only state for forked workers


def _encoder(preset):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found: install it (apt install ffmpeg / brew install ffmpeg / winget install ffmpeg)")
    t = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                        "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True)
    tail = ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if t.returncode == 0:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-profile:v", "high"] + preset["nvenc"] + tail
    return ["-c:v", "libx264", "-preset", "medium", "-profile:v", "high"] + preset["x264"] + tail


def _chunk(job):
    f0, f1, path = job
    st = _S
    view, F, ends, pix, mid = st["view"], st["F"], st["ends"], st["pix"], st["mid"]
    W, H = view.W, view.H                       # map area
    CH, y_map = st["canvas_h"], st["y_map"]     # full frame height, map offset inside it
    edge_arr, trip_arr, kind_arr, when = st["edge"], st["trip"], st["kind"], st["when"]
    lut_w = {k: (ramp(np.linspace(0, 1, 1024), s) * np.linspace(0, 1, 1024)[:, None]).astype(np.float32)
             for k, s in (("car", ORANGE), ("bike", TEAL))}
    end_at = lambda f: int(ends[min(f, F - 1)]) if f >= 0 else 0
    pixels = lambda a, b: np.concatenate([pix[e] for e in edge_arr[a:b]]) if b > a else np.empty(0, np.int64)

    start = end_at(f0 - 1)
    acc = {k: np.zeros(W * H, np.float32) for k in lut_w}
    for k in lut_w:
        sel = np.nonzero(kind_arr[:start] == k)[0]
        if len(sel):
            acc[k] += np.bincount(np.concatenate([pix[e] for e in edge_arr[sel]]), minlength=W * H)
    trail = np.zeros((W * H, 3), np.float32)
    inten = np.zeros(W * H, np.float32)

    def refresh(u):
        for k in lut_w:
            a = acc[k][u]
            m = a > 0
            if m.any():
                i_ = 0.3 + 0.7 * np.clip(np.log1p(a[m]) / st["ref"], 0, 1)
                trail[u[m]] = np.maximum(trail[u[m]], lut_w[k][(i_ * 1023).astype(int)])
                inten[u[m]] = np.maximum(inten[u[m]], i_)

    lit = np.nonzero(acc["car"] + acc["bike"])[0]
    if len(lit):
        refresh(lit)
    recent = np.zeros(W * H, np.float32)
    for f in range(max(0, f0 - RECENT_REPLAY), f0):
        recent *= RECENT_DECAY
        recent[pixels(end_at(f - 1), end_at(f))] = 1.0
    seen, prev = set(trip_arr[:start].tolist()), start
    ts = st["text"]
    fb, fm, fs = font(int(W / 22 * ts), True), font(int(W / 40 * ts)), font(int(W / 56 * ts))
    years = st["years"]
    t_start = pd.Timestamp(f"{years[0]}-01-01")
    t_span = pd.Timestamp(f"{years[-1] + 1}-01-01") - t_start
    ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{CH}",
                           "-r", str(st["fps"]), "-i", "-"] + st["enc"] + [path], stdin=subprocess.PIPE)
    portrait = CH > H * 1.15
    frame = np.empty((CH, W, 3), np.float32)
    frame[:] = BG
    rgb = frame[y_map:y_map + H]
    for f in range(f0, f1):
        cur = end_at(f)
        recent *= RECENT_DECAY if f < F else 0.8
        if cur > prev:
            for k in lut_w:
                sel = prev + np.nonzero(kind_arr[prev:cur] == k)[0]
                if len(sel):
                    np.add.at(acc[k], np.concatenate([pix[e] for e in edge_arr[sel]]), 1)
            u = np.unique(pixels(prev, cur))
            refresh(u)
            recent[u] = 1.0
            seen.update(trip_arr[prev:cur].tolist())
            prev = cur
        g = glow(inten.reshape(H, W), view)
        np.add(st["bg"], trail.reshape(H, W, 3), out=rgb)
        rgb += lut_w["car"][(np.clip(g, 0, 1) * 1023).astype(int)] * 0.55
        rgb += recent.reshape(H, W, 1) * RECENT_RGB
        img = Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8))
        d = ImageDraw.Draw(img)
        i = max(prev - 1, 0)
        if f < F:
            x, y = mid[edge_arr[i]]
            y += y_map
            s = W / 110 * max(1.0, ts * 0.9)
            d.rounded_rectangle([x - 1.6 * s, y - .55 * s, x + 1.6 * s, y + .45 * s], radius=.35 * s, fill=(90, 220, 255))
            d.rounded_rectangle([x - .9 * s, y - 1.15 * s, x + .8 * s, y - .4 * s], radius=.3 * s, fill=(90, 220, 255))
            for wx in (-.95, .95):
                d.ellipse([x + wx * s - .38 * s, y + .15 * s, x + wx * s + .38 * s, y + .9 * s], fill=(15, 15, 20))
        t = pd.Timestamp(when[i])
        pad = W // 36
        date_s, time_s = t.strftime("%a %d %b %Y"), t.strftime("%H:%M")
        km_s, trips_s = f"{st['km'][i]:,.0f} km", f"{len(seen):,} trips"
        if portrait:      # tall frame: stack the header, centred, in the band above the map
            y = max(pad, (y_map - (fb.size * 2.6 + fm.size * 2.4)) // 2)
            d.text((W // 2, y), date_s, font=fb, fill=(240, 240, 245), anchor="ma")
            d.text((W // 2, y + fb.size * 1.2), time_s, font=fm, fill=(150, 210, 255), anchor="ma")
            y += fb.size * 1.2 + fm.size * 1.9
            d.text((W // 2, y), km_s, font=fb, fill=(255, 190, 90), anchor="ma")
            d.text((W // 2, y + fb.size * 1.2), trips_s, font=fm, fill=(200, 200, 210), anchor="ma")
        else:
            d.text((pad, pad), date_s, font=fb, fill=(240, 240, 245))
            d.text((pad, pad + fb.size * 1.25), time_s, font=fm, fill=(150, 210, 255))
            d.text((W - pad, pad), km_s, font=fb, fill=(255, 190, 90), anchor="ra")
            d.text((W - pad, pad + fb.size * 1.25), trips_s, font=fm, fill=(200, 200, 210), anchor="ra")
        y0, x0, x1 = CH - pad - fs.size * 2, pad, W - pad
        lw = max(2, int(W / 270 * ts))
        d.line([(x0, y0), (x1, y0)], fill=(70, 75, 90), width=lw)
        d.line([(x0, y0), (x0 + (x1 - x0) * ((t - t_start) / t_span), y0)], fill=(255, 160, 40), width=lw)
        label_w = d.textlength("2000", font=fs)
        step = max(1, int(np.ceil(len(years) * label_w * 1.6 / (x1 - x0))))   # thin labels if they would touch
        for yr in years[::step]:
            xx = x0 + (x1 - x0) * ((pd.Timestamp(f"{yr}-01-01") - t_start) / t_span)
            xx = min(max(xx, x0 + label_w / 2), x1 - label_w / 2)            # keep labels inside the frame
            d.text((xx, y0 + lw + 4), str(yr), font=fs, fill=(130, 135, 150), anchor="ma")
        if f >= F:
            ty = (y_map + H + y0) // 2 if portrait else y0 - fm.size * 2.2
            d.text((W // 2, ty), st["title"], font=fm, fill=(235, 235, 240), anchor="mm" if portrait else "ma")
        ff.stdin.write(img.tobytes())
    ff.stdin.close()
    ff.wait()
    return path


def video(G, tv, out, preset="hq", area=None, seconds=None, workers=None, title="Every drive in Bangalore",
          progress=None):
    """Render the time-lapse to `out` (.mp4) using a preset from PRESETS. Returns the path."""
    P = PRESETS[preset]
    out = Path(out)
    tv = tv.sort_values(["t_enter"]).reset_index(drop=True)
    bbox = CORE_BBOX if (area or P["area"]) == "core" else C.BBOX
    view = View(bbox, P["width"])
    canvas_h = max(view.H, P["height"] or 0)
    canvas_h -= canvas_h % 2
    y_map = (canvas_h - view.H) // 2
    frames = (seconds or P["seconds"]) * P["fps"]
    if progress:
        progress(0, 3, "drawing the background")
    old_bbox, C.BBOX = C.BBOX, bbox            # background() frames by C.BBOX
    try:
        bg = background(G, view)
    finally:
        C.BBOX = old_bbox
    brush = max(2, round(P["width"] * P["line"]))
    pix, mid = edge_pixels(G, tv.edge.unique(), view, brush)
    final = np.zeros(view.W * view.H, np.float32)
    for e, c in tv.edge.value_counts().items():
        final[pix[e]] += c
    N = len(tv)
    local = pd.to_datetime(tv.date) + pd.to_timedelta(tv.hour, unit="h")
    _S.update(view=view, F=frames, fps=P["fps"], enc=_encoder(P), bg=bg, pix=pix, mid=mid, title=title,
              canvas_h=canvas_h, y_map=y_map, text=P["text"],
              ends=np.minimum(N, np.round(N * (np.arange(1, frames + 1) / frames) ** PACE).astype(int)),
              ref=np.log1p(np.quantile(final[final > 0], 0.995)), edge=tv.edge.values, trip=tv.trip_id.values,
              kind=tv.kind.values, when=local.dt.strftime("%Y-%m-%d %H:%M").values,
              km=np.cumsum(tv.length_m.values) / 1000, years=sorted(tv.year.unique()))
    total = frames + HOLD_S * P["fps"]
    can_fork = "fork" in mp.get_all_start_methods()
    workers = workers or (max(1, min(3, (os.cpu_count() or 2) // 2)) if can_fork else 1)
    cuts = np.linspace(0, total, workers + 1).round().astype(int)
    jobs = [(int(a), int(b), str(out.with_name(f".{out.stem}_part{k}.mp4"))) for k, (a, b) in enumerate(zip(cuts[:-1], cuts[1:]))]
    if progress:
        progress(1, 3, f"rendering {total} frames at {view.W}x{canvas_h} on {workers} worker(s)")
    if workers > 1:
        with mp.get_context("fork").Pool(workers) as pool:
            parts = pool.map(_chunk, jobs)
    else:
        parts = [_chunk(j) for j in jobs]
    lst = out.with_name(f".{out.stem}_parts.txt")
    lst.write_text("".join(f"file '{Path(p).name}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                    "-movflags", "+faststart", str(out)], check=True)
    for p in parts:
        Path(p).unlink()
    lst.unlink()
    if progress:
        progress(3, 3, f"saved {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    return out
