"""Command line.

  python -m bdrives run Timeline.json           load + clean + map-match + report + map image (output/)
  python -m bdrives report                      reprint the report with other filters
  python -m bdrives image                       redraw the glow map
  python -m bdrives video --preset reddit       render the time-lapse (needs ffmpeg); presets: hq, reddit, whatsapp

Filters (report / image / video): --no-bikes  --keep-airport-cabs  --years 2018-2026
"""
import argparse
import sys
import time
from pathlib import Path

from . import config as C


def _filters(p):
    p.add_argument("--out", default="output", help="output folder (default: output/)")
    p.add_argument("--no-bikes", action="store_true", help="cars only")
    p.add_argument("--keep-airport-cabs", action="store_true", help="keep airport rides on flight days")
    p.add_argument("--years", help="e.g. 2018-2026")


def _load(args):
    from . import stats
    db = Path(args.out) / "timeline.db"
    if not db.exists():
        sys.exit(f"{db} not found: run `python -m bdrives run <your Timeline.json>` first")
    years = tuple(int(y) for y in args.years.split("-")) if args.years else None
    kinds = ("car",) if args.no_bikes else ("car", "bike")
    trips, tv = stats.load(db, kinds, not args.keep_airport_cabs, years)
    if trips.empty:
        sys.exit("no matched trips for these filters")
    return db, trips, tv


def _progress(done, total, msg):
    bar = int(30 * done / max(total, 1))
    print(f"\r  [{'#' * bar}{'.' * (30 - bar)}] {msg:<60}", end="" if done < total else "\n", flush=True)


def main():
    ap = argparse.ArgumentParser(prog="bdrives", description="Bangalore driving analysis from a Google Maps Timeline export")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process an export end to end")
    r.add_argument("timeline", help="Timeline.json (or a .zip containing it)")
    r.add_argument("--fast", action="store_true", help="one matching pass instead of two (about 2x quicker)")
    _filters(r)
    for name in ("report", "image", "video"):
        s = sub.add_parser(name)
        _filters(s)
        if name == "video":
            s.add_argument("--preset", default="hq", choices=["hq", "reddit", "whatsapp"],
                           help="hq: 1920 square; reddit: 1080 square, thicker lines; whatsapp: 720x1280 vertical, <16 MB")
            s.add_argument("--area", choices=["full", "core"], help="full Greater Bangalore or the city core (preset default)")
            s.add_argument("--seconds", type=int, help="length before the final hold (preset default)")
    args = ap.parse_args()

    from . import match, render, stats
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "timeline.db"

    if args.cmd == "run":
        from .load import load
        t0 = time.time()
        print(f"Reading {args.timeline} ...")
        counts = load(args.timeline, db)
        print("  " + ", ".join(f"{v:,} {k.replace('_', ' ')}" for k, v in counts.items()))
        print("Matching trips to roads (single process; ~2-15 min depending on history and machine)")
        match.run(db, bikes=not args.no_bikes, passes=1 if args.fast else 2, progress=_progress)
        print(f"Done in {time.time() - t0:.0f}s")
        args.cmd = "image+report"

    db, trips, tv = _load(args)
    opts = (f"{'cars' if args.no_bikes else 'cars + bikes'}, airport cabs "
            f"{'kept' if args.keep_airport_cabs else 'excluded'}{', years ' + args.years if args.years else ''}")
    if args.cmd in ("report", "image+report"):
        md = stats.report_md(stats.summary(trips, tv), opts)
        (out / "report.md").write_text(md)
        print("\n" + md + f"\n(saved to {out / 'report.md'})")
    if args.cmd in ("image", "image+report"):
        G = match.Graph()
        path = render.image(G, trips, tv, out / "map.png")
        print(f"map image: {path}")
    if args.cmd == "video":
        G = match.Graph()
        path = render.video(G, tv, out / f"drives_{args.preset}.mp4", preset=args.preset, area=args.area,
                            seconds=args.seconds, progress=_progress)
        print(f"video: {path}")


if __name__ == "__main__":
    main()
