#!/usr/bin/env python3
"""
PTV train departures tool for Supernova.

Cache update (run via cron, e.g. weekly):
    python3 ptv_trains.py --update-cache

Direct test:
    python3 ptv_trains.py --test YOUR_TOKEN

YOU MUST HAVE AN API KEY - sign up at opendata.transport.vic.gov.au
"""

import sys, io, csv, zipfile, json, os, requests
from datetime import datetime, timezone, date, timedelta
from google.transit import gtfs_realtime_pb2
import zoneinfo

GTFS_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
    "fb152201-859f-4882-9206-b768060b50ad/download/gtfs.zip"
)
RT_URL   = "https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/metro/trip-updates"
MELB_TZ  = zoneinfo.ZoneInfo("Australia/Melbourne")


def build_cache(stop_id: str, gtfs_zip_folder: str, cache_file: str):
    """Download static GTFS and save a small cache for one stop. Takes ~30s."""
    print(f"Downloading static GTFS timetable (~213MB)...", flush=True)
    outer = zipfile.ZipFile(io.BytesIO(requests.get(GTFS_URL, timeout=180).content))
    inner = zipfile.ZipFile(io.BytesIO(outer.read(f"{gtfs_zip_folder}/google_transit.zip")))

    calendar = {}
    with inner.open("calendar.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            calendar[row["service_id"]] = row

    calendar_dates = {}
    with inner.open("calendar_dates.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            calendar_dates.setdefault(row["date"], []).append(row)

    trips = {}
    with inner.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            trips[row["trip_id"]] = {
                "service_id":    row["service_id"],
                "trip_headsign": row.get("trip_headsign", ""),
                "route_id":      row.get("route_id", ""),
            }

    # attempting to capture all 
    FLINDERS_STOP_IDS = {
        "11212", "11213", "11214", "11215", "11216", "11217", "11218",
        "12201", "12202", "12203", "12204", "12205", "22238"
    }

    stop_times_by_trip = {}
    with inner.open("stop_times.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            tid = row["trip_id"]
            if row["stop_id"] == stop_id:
                stop_times_by_trip.setdefault(tid, {})["departure_time"] = row["departure_time"]
            elif row["stop_id"] in FLINDERS_STOP_IDS:
                stop_times_by_trip.setdefault(tid, {})["flinders_arrival"] = row["arrival_time"]

    stop_times = [
        {"trip_id": tid, **times}
        for tid, times in stop_times_by_trip.items()
        if "departure_time" in times and "flinders_arrival" in times
    ]

    cache = {
        "built":          datetime.now(tz=MELB_TZ).isoformat(),
        "stop_id":        stop_id,
        "calendar":       calendar,
        "calendar_dates": calendar_dates,
        "trips":          trips,
        "stop_times":     stop_times,
    }
    with open(cache_file, "w") as f:
        json.dump(cache, f)
    print(f"Cache saved: {cache_file}")
    print(f"  {len(stop_times)} departures for stop {stop_id}, {len(trips)} trips")


def _parse_gtfs_time(time_str: str, base_date: date) -> float:
    h, m, s = map(int, time_str.split(":"))
    return (
        datetime(base_date.year, base_date.month, base_date.day, tzinfo=MELB_TZ)
        + timedelta(hours=h, minutes=m, seconds=s)
    ).timestamp()


def get_departures(api_key: str, stop_id: str, stop_name: str, cache_file: str, n: int = 3) -> list[dict]:
    """
    Returns a list of the next n departures from the configured stop.
    Each dict has: scheduled_time, actual_time, minutes_away, headsign, delay_s, realtime (bool)
    Raises FileNotFoundError if cache doesn't exist yet.
    """
    with open(cache_file) as f:
        cache = json.load(f)

    now_melb  = datetime.now(tz=MELB_TZ)
    now_ts    = now_melb.timestamp()
    today     = now_melb.date()
    today_str = today.strftime("%Y%m%d")
    today_dow = today.strftime("%A").lower()

    # Active services today
    active_services = set()
    for sid, row in cache["calendar"].items():
        if row.get(today_dow) == "1" and row["start_date"] <= today_str <= row["end_date"]:
            active_services.add(sid)
    for exc in cache["calendar_dates"].get(today_str, []):
        if exc["exception_type"] == "1":
            active_services.add(exc["service_id"])
        elif exc["exception_type"] == "2":
            active_services.discard(exc["service_id"])

    # Scheduled departures
    scheduled = []
    for st in cache["stop_times"]:
        trip = cache["trips"].get(st["trip_id"])
        if not trip or trip["service_id"] not in active_services:
            continue
        dep_ts = _parse_gtfs_time(st["departure_time"], today)
        if dep_ts < now_ts - 60:
            continue
        scheduled.append({
            "trip_id":        st["trip_id"],
            "scheduled_ts":   dep_ts,
            "actual_ts":      dep_ts,
            "delay_s":        0,
            "realtime":       False,
            "headsign":       trip["trip_headsign"],
            "flinders_ts":    _parse_gtfs_time(st["flinders_arrival"], today) if "flinders_arrival" in st else None,
        })
    scheduled.sort(key=lambda x: x["scheduled_ts"])

    # Overlay realtime delays
    r = requests.get(RT_URL, headers={"KeyID": api_key}, timeout=15)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        for stu in tu.stop_time_update:
            if stu.stop_id != stop_id:
                continue
            for dep in scheduled:
                if dep["trip_id"] == tu.trip.trip_id:
                    dep["delay_s"]  = stu.departure.delay
                    dep["actual_ts"] = dep["scheduled_ts"] + dep["delay_s"]
                    dep["realtime"] = True

    result = []
    for dep in scheduled[:n]:
        dt = datetime.fromtimestamp(dep["actual_ts"], tz=MELB_TZ)
        mins = max(0, int((dep["actual_ts"] - now_ts) / 60))
        flinders_dt = datetime.fromtimestamp(dep["flinders_ts"], tz=MELB_TZ) if dep.get("flinders_ts") else None
        result.append({
            "time":              dt.strftime("%H:%M"),
            "minutes":           mins,
            "headsign":          dep["headsign"],
            "delay_s":           dep["delay_s"],
            "realtime":          dep["realtime"],
            "flinders_arrival":  flinders_dt.strftime("%H:%M") if flinders_dt else None,
        })
    return result

def get_departures_by_arrival(api_key: str, stop_id: str, stop_name: str, cache_file: str, target_arrival: datetime, n: int = 3) -> list[dict]:
    """
    Returns up to n departures that arrive at Flinders Street by target_arrival.
    target_arrival should be a timezone-aware datetime in MELB_TZ.
    """
    with open(cache_file) as f:
        cache = json.load(f)

    now_melb  = datetime.now(tz=MELB_TZ)
    now_ts    = now_melb.timestamp()
    today     = now_melb.date()
    today_str = today.strftime("%Y%m%d")
    today_dow = today.strftime("%A").lower()

    # Active services today
    active_services = set()
    for sid, row in cache["calendar"].items():
        if row.get(today_dow) == "1" and row["start_date"] <= today_str <= row["end_date"]:
            active_services.add(sid)
    for exc in cache["calendar_dates"].get(today_str, []):
        if exc["exception_type"] == "1":
            active_services.add(exc["service_id"])
        elif exc["exception_type"] == "2":
            active_services.discard(exc["service_id"])

    target_ts = target_arrival.timestamp()

    # Scheduled departures that arrive at Flinders by target time
    scheduled = []
    for st in cache["stop_times"]:
        if "flinders_arrival" not in st:
            continue
        trip = cache["trips"].get(st["trip_id"])
        if not trip or trip["service_id"] not in active_services:
            continue
        dep_ts      = _parse_gtfs_time(st["departure_time"], today)
        flinders_ts = _parse_gtfs_time(st["flinders_arrival"], today)
        if dep_ts < now_ts - 60:
            continue
        if flinders_ts > target_ts:
            continue
        scheduled.append({
            "trip_id":      st["trip_id"],
            "scheduled_ts": dep_ts,
            "actual_ts":    dep_ts,
            "flinders_ts":  flinders_ts,
            "delay_s":      0,
            "realtime":     False,
            "headsign":     trip["trip_headsign"],
        })
    scheduled.sort(key=lambda x: x["scheduled_ts"])

    # Overlay realtime delays
    r = requests.get(RT_URL, headers={"KeyID": api_key}, timeout=15)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        for stu in tu.stop_time_update:
            if stu.stop_id != stop_id:
                continue
            for dep in scheduled:
                if dep["trip_id"] == tu.trip.trip_id:
                    dep["delay_s"]   = stu.departure.delay
                    dep["actual_ts"] = dep["scheduled_ts"] + dep["delay_s"]
                    dep["flinders_ts"] = dep["flinders_ts"] + dep["delay_s"]
                    dep["realtime"]  = True

    # Re-filter after delay overlay — a delayed train may no longer arrive in time
    scheduled = [d for d in scheduled if d["flinders_ts"] <= target_ts]

    result = []
    for dep in scheduled[:n]:
        dt          = datetime.fromtimestamp(dep["actual_ts"], tz=MELB_TZ)
        flinders_dt = datetime.fromtimestamp(dep["flinders_ts"], tz=MELB_TZ)
        mins        = max(0, int((dep["actual_ts"] - now_ts) / 60))
        mins_early  = max(0, int((target_ts - dep["flinders_ts"]) / 60))
        result.append({
            "time":             dt.strftime("%H:%M"),
            "minutes":          mins,
            "headsign":         dep["headsign"],
            "delay_s":          dep["delay_s"],
            "realtime":         dep["realtime"],
            "flinders_arrival": flinders_dt.strftime("%H:%M"),
            "minutes_early":    mins_early,
        })
    return result

def format_departures(departures: list[dict], stop_name: str, walk_minutes: int = 0) -> str:
    """Format departure list into a natural voice-friendly string."""
    if not departures:
        return f"No upcoming departures found from {stop_name}."

    lines = [f"Next trains from {stop_name}:"]
    for d in departures:
        # AM/PM time format, strip leading zero (e.g. "8:45 AM" not "08:45 AM")
        dt = datetime.strptime(d["time"], "%H:%M")
        time_str = dt.strftime("%I:%M %p").lstrip("0")

        mins_str = "less than a minute" if d["minutes"] == 0 else f"{d['minutes']} minute{'s' if d['minutes'] != 1 else ''}"
        delay_str = f", running {d['delay_s'] // 60} minutes late" if d["delay_s"] > 60 else ""
        rt_str = "" if d["realtime"] else " (scheduled)"

        if walk_minutes > 0 and d["minutes"] < walk_minutes:
            warn_str = f" WARNING: Not enough time — it takes {walk_minutes} min to walk to the station! Tell user this, and recommend next train departure! "
        else:
            walk_str = f" ({walk_minutes} min walk to station)" if walk_minutes > 0 else ""
            warn_str = walk_str

        early_str = f", arrives Flinders Street {d['minutes_early']} min before target" if d.get("minutes_early") is not None else ""

        lines.append(f"  {time_str} — in {mins_str}{delay_str}{rt_str} to {d['headsign']}{warn_str}{early_str}")

    if walk_minutes > 0:
        lines.append(f"\nReminder: walking to {stop_name} takes {walk_minutes} minutes.")

    return "\n".join(lines)


# --- CLI ---
if __name__ == "__main__":
    if "--update-cache" in sys.argv:
        # Load from project config
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from config.settings import load_config
        cfg = load_config()
        if not cfg.ptv:
            print("ERROR: No [ptv] section in settings.yaml")
            sys.exit(1)
        build_cache(cfg.ptv.stop_id, cfg.ptv.gtfs_zip_folder, cfg.ptv.cache_file)

    elif "--test" in sys.argv:
        token = sys.argv[sys.argv.index("--test") + 1]
        cache = os.path.join(os.path.dirname(__file__), "../config/ptv_cache.json")
        deps = get_departures(token, "14312", "Anstey Station", cache, n=3)
        print(format_departures(deps, "Anstey Station"))

    elif "--find-stop" in sys.argv:
        query = sys.argv[sys.argv.index("--find-stop") + 1]
        # load project config
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from config.settings import load_config
        cfg = load_config()
        print(f"Downloading GTFS to search for '{query}'...")
        outer = zipfile.ZipFile(io.BytesIO(requests.get(GTFS_URL, timeout=180).content))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(f"{cfg.ptv.gtfs_zip_folder}/google_transit.zip")))
        with inner.open("stops.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                if query.lower() in row["stop_name"].lower():
                    print(f"  {row['stop_id']}  {row['stop_name']}  {row.get('platform_code', '')}  {row.get('stop_desc', '')}")
    
    elif "--test-arrival" in sys.argv:
        target_str = sys.argv[sys.argv.index("--test-arrival") + 1]  # e.g. "09:00"
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from config.settings import load_config
        cfg = load_config()
        cache = cfg.ptv.cache_file
        target = datetime.now(tz=MELB_TZ).replace(
            hour=int(target_str.split(":")[0]),
            minute=int(target_str.split(":")[1]),
            second=0, microsecond=0
        )
        deps = get_departures_by_arrival(cfg.ptv.api_key, cfg.ptv.stop_id, cfg.ptv.stop_name, cache, target, n=3)
        print(format_departures(deps, cfg.ptv.stop_name, walk_minutes=cfg.ptv.walk_minutes))

    else:
        print(__doc__)