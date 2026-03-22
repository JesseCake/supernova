"""
PTV train departures tool for Supernova.

This file contains everything needed for PTV train departures:
  - Schema functions (passed to Ollama as tool definitions)
  - Core library functions (GTFS parsing, realtime overlays, formatting)
  - Executors (called by core when the LLM invokes the tool)
  - TOOLS export list (tells the tool loader about both tools in this file)

To update the timetable cache, use the CLI script:
    python3 scripts/ptv_cache_update.py --update-cache

All config (api_key, stop details, cache location) comes from
config/ptv_departures.yaml via tool_config — AppConfig is not used.
"""

import sys, io, csv, zipfile, json, os, requests
from typing import Annotated
from pydantic import Field
from datetime import datetime, date, timedelta, timezone
from google.transit import gtfs_realtime_pb2
import zoneinfo

from core.tool_base import ToolBase

log = ToolBase.logger('ptv_departures')

# ── Constants ─────────────────────────────────────────────────────────────────

GTFS_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
    "fb152201-859f-4882-9206-b768060b50ad/download/gtfs.zip"
)
RT_URL  = "https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/metro/trip-updates"
MELB_TZ = zoneinfo.ZoneInfo("Australia/Melbourne")

# All known Flinders Street platform stop IDs — used to find arrival times
FLINDERS_STOP_IDS = {
    "11212", "11213", "11214", "11215", "11216", "11217", "11218",
    "12201", "12202", "12203", "12204", "12205", "22238"
}


# ── Schema functions ──────────────────────────────────────────────────────────
# These are passed to Ollama as tool definitions. They describe the interface
# the LLM uses to call the tool — the actual logic lives in the executors below.

def get_next_train_departures(
    count: Annotated[int, Field(default=2, description="Number of upcoming departures to return. Default is 2.")] = 2,
) -> str:
    """
    Get the next train departure times from the local station to the city.
    Use when the user asks about trains, catching a train, or getting to the city.
    """
    ...


def get_train_departures_by_arrival(
    arrival_time: Annotated[str, Field(description="Target arrival time at Flinders Street in 24-hour HH:MM format e.g. '09:00' or '14:30'. Required.")],
    count: Annotated[int, Field(default=2, description="Number of suitable departures to return. Default is 2.")] = 2,
) -> str:
    """
    Get train departures that will arrive in the city by a specified time today.
    Use when the user wants to arrive somewhere by a certain time.
    """
    ...


# ── Library functions ─────────────────────────────────────────────────────────
# Core GTFS/realtime logic. Also used by scripts/ptv_cache_update.py.

def build_cache(stop_id: str, gtfs_zip_folder: str, cache_file: str):
    """
    Download static GTFS timetable and save a small cache for one stop.
    Takes ~30s due to the large GTFS download (~213MB).
    Run via scripts/ptv_cache_update.py --update-cache, ideally weekly via cron.
    """
    log.info("Downloading static GTFS timetable (~213MB)")
    outer = zipfile.ZipFile(io.BytesIO(requests.get(GTFS_URL, timeout=180).content))
    inner = zipfile.ZipFile(io.BytesIO(outer.read(f"{gtfs_zip_folder}/google_transit.zip")))

    # Load service calendar — maps service_id to days of week and date range
    calendar = {}
    with inner.open("calendar.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            calendar[row["service_id"]] = row

    # Load calendar exceptions — additions/removals for specific dates (e.g. public holidays)
    calendar_dates = {}
    with inner.open("calendar_dates.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            calendar_dates.setdefault(row["date"], []).append(row)

    # Load trip metadata
    trips = {}
    with inner.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            trips[row["trip_id"]] = {
                "service_id":    row["service_id"],
                "trip_headsign": row.get("trip_headsign", ""),
                "route_id":      row.get("route_id", ""),
            }

    # Build stop_times: only keep trips that call at our stop AND Flinders Street
    stop_times_by_trip = {}
    with inner.open("stop_times.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            tid = row["trip_id"]
            if row["stop_id"] == stop_id:
                stop_times_by_trip.setdefault(tid, {})["departure_time"] = row["departure_time"]
            elif row["stop_id"] in FLINDERS_STOP_IDS:
                stop_times_by_trip.setdefault(tid, {})["flinders_arrival"] = row["arrival_time"]

    # Only keep trips that have both our stop departure and a Flinders arrival
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
    log.info("Cache saved", extra={'data': f"{cache_file} — {len(stop_times)} departures, {len(trips)} trips"})


def _parse_gtfs_time(time_str: str, base_date: date) -> float:
    """
    Convert a GTFS time string (HH:MM:SS, may exceed 24h for overnight services)
    to a UTC timestamp anchored to base_date in Melbourne time.
    """
    h, m, s = map(int, time_str.split(":"))
    return (
        datetime(base_date.year, base_date.month, base_date.day, tzinfo=MELB_TZ)
        + timedelta(hours=h, minutes=m, seconds=s)
    ).timestamp()


def get_departures(api_key: str, stop_id: str, stop_name: str, cache_file: str, n: int = 3) -> list[dict]:
    """
    Return a list of the next n departures from stop_id.
    Overlays realtime delay data from the PTV GTFS-RT feed.
    Raises FileNotFoundError if the cache hasn't been built yet.
    """
    with open(cache_file) as f:
        cache = json.load(f)

    now_melb  = datetime.now(tz=MELB_TZ)
    now_ts    = now_melb.timestamp()
    today     = now_melb.date()
    today_str = today.strftime("%Y%m%d")
    today_dow = today.strftime("%A").lower()

    # Determine which services run today
    active_services = set()
    for sid, row in cache["calendar"].items():
        if row.get(today_dow) == "1" and row["start_date"] <= today_str <= row["end_date"]:
            active_services.add(sid)
    # Apply exceptions (public holidays etc)
    for exc in cache["calendar_dates"].get(today_str, []):
        if exc["exception_type"] == "1":
            active_services.add(exc["service_id"])
        elif exc["exception_type"] == "2":
            active_services.discard(exc["service_id"])

    # Build list of upcoming scheduled departures
    scheduled = []
    for st in cache["stop_times"]:
        trip = cache["trips"].get(st["trip_id"])
        if not trip or trip["service_id"] not in active_services:
            continue
        dep_ts = _parse_gtfs_time(st["departure_time"], today)
        if dep_ts < now_ts - 60:  # skip trains that left more than 1 min ago
            continue
        scheduled.append({
            "trip_id":      st["trip_id"],
            "scheduled_ts": dep_ts,
            "actual_ts":    dep_ts,
            "delay_s":      0,
            "realtime":     False,
            "headsign":     trip["trip_headsign"],
            "flinders_ts":  _parse_gtfs_time(st["flinders_arrival"], today) if "flinders_arrival" in st else None,
        })
    scheduled.sort(key=lambda x: x["scheduled_ts"])

    # Overlay realtime delays from GTFS-RT feed
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
                    dep["realtime"]  = True

    # Format results
    result = []
    for dep in scheduled[:n]:
        dt         = datetime.fromtimestamp(dep["actual_ts"], tz=MELB_TZ)
        mins       = max(0, int((dep["actual_ts"] - now_ts) / 60))
        flinders_dt = datetime.fromtimestamp(dep["flinders_ts"], tz=MELB_TZ) if dep.get("flinders_ts") else None
        result.append({
            "time":             dt.strftime("%H:%M"),
            "minutes":          mins,
            "headsign":         dep["headsign"],
            "delay_s":          dep["delay_s"],
            "realtime":         dep["realtime"],
            "flinders_arrival": flinders_dt.strftime("%H:%M") if flinders_dt else None,
        })
    return result


def get_departures_by_arrival(api_key: str, stop_id: str, stop_name: str, cache_file: str, target_arrival: datetime, n: int = 3) -> list[dict]:
    """
    Return up to n departures that arrive at Flinders Street by target_arrival.
    target_arrival must be a timezone-aware datetime in MELB_TZ.
    Re-filters after delay overlay so delayed trains that miss the target are excluded.
    """
    with open(cache_file) as f:
        cache = json.load(f)

    now_melb  = datetime.now(tz=MELB_TZ)
    now_ts    = now_melb.timestamp()
    today     = now_melb.date()
    today_str = today.strftime("%Y%m%d")
    today_dow = today.strftime("%A").lower()
    target_ts = target_arrival.timestamp()

    # Determine which services run today
    active_services = set()
    for sid, row in cache["calendar"].items():
        if row.get(today_dow) == "1" and row["start_date"] <= today_str <= row["end_date"]:
            active_services.add(sid)
    for exc in cache["calendar_dates"].get(today_str, []):
        if exc["exception_type"] == "1":
            active_services.add(exc["service_id"])
        elif exc["exception_type"] == "2":
            active_services.discard(exc["service_id"])

    # Build list of scheduled departures that arrive at Flinders by target time
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
                    dep["delay_s"]     = stu.departure.delay
                    dep["actual_ts"]   = dep["scheduled_ts"] + dep["delay_s"]
                    dep["flinders_ts"] = dep["flinders_ts"]  + dep["delay_s"]
                    dep["realtime"]    = True

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
    """Format a departure list into a natural, voice-friendly string."""
    if not departures:
        return f"No upcoming departures found from {stop_name}."

    lines = [f"Next trains from {stop_name}:"]
    for d in departures:
        # Format time as "8:45 AM" (no leading zero)
        dt       = datetime.strptime(d["time"], "%H:%M")
        time_str = dt.strftime("%I:%M %p").lstrip("0")

        mins_str  = "less than a minute" if d["minutes"] == 0 else f"{d['minutes']} minute{'s' if d['minutes'] != 1 else ''}"
        delay_str = f", running {d['delay_s'] // 60} minutes late" if d["delay_s"] > 60 else ""
        rt_str    = "" if d["realtime"] else " (scheduled)"

        # Walk time warning — if not enough time to walk to station, flag it
        if walk_minutes > 0 and d["minutes"] < walk_minutes:
            warn_str = (
                f" WARNING: Not enough time — it takes {walk_minutes} min to walk to the station! "
                f"Tell user this, and recommend next train departure!"
            )
        else:
            warn_str = f" ({walk_minutes} min walk to station)" if walk_minutes > 0 else ""

        early_str = (
            f", arrives Flinders Street {d['minutes_early']} min before target"
            if d.get("minutes_early") is not None else ""
        )

        lines.append(f"  {time_str} — in {mins_str}{delay_str}{rt_str} to {d['headsign']}{warn_str}{early_str}")

    if walk_minutes > 0:
        lines.append(f"\nReminder: walking to {stop_name} takes {walk_minutes} minutes.")

    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_cache_file(tool_config: dict) -> str:
    """
    Return the cache file path from tool_config, falling back to ./cache/ptv_cache.json.
    Also ensures the cache directory exists.
    """
    cache_file = tool_config.get('cache_file') or os.path.join(
        os.path.dirname(__file__), '../cache/ptv_cache.json'
    )
    os.makedirs(os.path.dirname(os.path.abspath(cache_file)), exist_ok=True)
    return cache_file


# ── Executors ─────────────────────────────────────────────────────────────────
# Called by core when the LLM invokes the tool.
# All config is read from tool_config (ptv_departures.yaml).

def execute_next_departures(tool_args: dict, session, core, tool_config: dict) -> str:
    count = ToolBase.params(tool_args).get('count', 2)
    ToolBase.speak(core, session, "Checking train times.")
    log.info("Fetching next departures", extra={'data': f"count={count}"})
    try:
        api_key    = tool_config.get('api_key')
        stop_id    = tool_config.get('stop_id', '14312')
        stop_name  = tool_config.get('stop_name', 'Anstey Station')
        walk_mins  = tool_config.get('walk_minutes', 7)
        cache_file = _get_cache_file(tool_config)

        deps   = get_departures(api_key, stop_id, stop_name, cache_file, n=count)
        result = format_departures(deps, stop_name, walk_mins)
        return ToolBase.result(core, 'get_next_train_departures', {"text": result})

    except FileNotFoundError:
        return ToolBase.error(core, 'get_next_train_departures',
            "Train timetable cache not found. Run scripts/ptv_cache_update.py --update-cache first.")
    except Exception as e:
        log.error("Failed to fetch next departures", exc_info=True)
        return ToolBase.error(core, 'get_next_train_departures', f"Error fetching train times: {e}")


def execute_by_arrival(tool_args: dict, session, core, tool_config: dict) -> str:
    params     = ToolBase.params(tool_args)
    target_str = params.get('arrival_time')
    count      = params.get('count', 3)
    ToolBase.speak(core, session, "Checking train arrival times.")
    log.info("Fetching departures by arrival", extra={'data': f"target={target_str} count={count}"})
    try:
        api_key    = tool_config.get('api_key')
        stop_id    = tool_config.get('stop_id', '14312')
        stop_name  = tool_config.get('stop_name', 'Anstey Station')
        walk_mins  = tool_config.get('walk_minutes', 7)
        cache_file = _get_cache_file(tool_config)

        target = datetime.now(tz=MELB_TZ).replace(
            hour=int(target_str.split(":")[0]),
            minute=int(target_str.split(":")[1]),
            second=0, microsecond=0
        )

        deps   = get_departures_by_arrival(api_key, stop_id, stop_name, cache_file, target, n=count)
        result = format_departures(deps, stop_name, walk_mins)

        if not deps:
            return ToolBase.result(core, 'get_train_departures_by_arrival', {
                "text": (
                    f"No trains from {stop_name} will arrive at Flinders Street by {target_str}. "
                    "Tell the user there are no suitable trains and suggest they check for a later "
                    "target time or seek alternate transport."
                )
            })

        return ToolBase.result(core, 'get_train_departures_by_arrival', {
            "text": result,
            "instructions": (
                "For each train option, tell the user what time it departs, and how many minutes early it will arrive at Flinders Street "
                "before their target time. If a train arrives very close to the target time (less than 2 minutes early) warn the user it is cutting it fine. "
                "If the train has a warning about walk time, tell the user they may miss this train and give how many minutes until departure."
            )
        })

    except FileNotFoundError:
        return ToolBase.error(core, 'get_train_departures_by_arrival',
            "Train timetable cache not found. Tell the user to run scripts/ptv_cache_update.py --update-cache first.")
    except Exception as e:
        log.error("Failed to fetch departures by arrival", exc_info=True)
        return ToolBase.error(core, 'get_train_departures_by_arrival', f"Error fetching train times: {e}")


# ── Multi-tool export ─────────────────────────────────────────────────────────
# Because this file defines two tools, we export them via a TOOLS list.
# The tool_loader checks for this list and registers each entry separately
# instead of looking for a single schema function named after the file.

TOOLS = [
    {
        'schema':  get_next_train_departures,
        'name':    'get_next_train_departures',
        'execute': execute_next_departures,
    },
    {
        'schema':  get_train_departures_by_arrival,
        'name':    'get_train_departures_by_arrival',
        'execute': execute_by_arrival,
    },
]