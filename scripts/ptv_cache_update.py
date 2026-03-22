#!/usr/bin/env python3
"""
PTV timetable cache updater for Supernova.

Run manually or via cron (e.g. weekly on Monday at 3am) to refresh the
static GTFS timetable cache used by the train departure tools.

Usage:
    .venv/bin/python3 scripts/ptv_cache_update.py --update-cache
    .venv/bin/python3 scripts/ptv_cache_update.py --test
    .venv/bin/python3 scripts/ptv_cache_update.py --test-arrival 09:00
    .venv/bin/python3 scripts/ptv_cache_update.py --find-stop "anstey"

Cron example (weekly, Monday 3am):
    0 3 * * 1  cd /home/user/supernova && /home/user/supernova/.venv/python3 scripts/ptv_cache_update.py --update-cache
(use crontab -e - change the directory above from /home/user to where you're keeping this)

Config is read from config/ptv_departures.yaml — no need to touch this script
when changing stops or API keys.
"""

import sys
import os
import yaml
from datetime import datetime
import zoneinfo

MELB_TZ = zoneinfo.ZoneInfo("Australia/Melbourne")

# Make sure we can import from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def load_ptv_config() -> dict:
    """Load PTV config from config/ptv_departures.yaml."""
    yaml_path = os.path.join(os.path.dirname(__file__), '../config/ptv_departures.yaml')
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f)

    # Resolve cache_file — use yaml value or fall back to data/ptv_departures/ptv_cache.json
    if not cfg.get('cache_file'):
        from core.tool_base import ToolBase
        cfg['cache_file'] = ToolBase.data_path('ptv_departures', 'ptv_cache.json')

    return cfg


if __name__ == "__main__":
    # Import library functions from the tool file
    from tools.ptv_departures import (
        build_cache,
        get_departures,
        get_departures_by_arrival,
        format_departures,
        GTFS_URL,
    )

    if "--update-cache" in sys.argv:
        # Download fresh GTFS timetable and rebuild the cache
        cfg = load_ptv_config()
        print(f"Updating cache for stop {cfg['stop_id']} ({cfg['stop_name']})...")
        build_cache(cfg['stop_id'], cfg['gtfs_zip_folder'], cfg['cache_file'])

    elif "--test" in sys.argv:
        # Test next departures using the current cache
        cfg = load_ptv_config()
        deps = get_departures(cfg['api_key'], cfg['stop_id'], cfg['stop_name'], cfg['cache_file'], n=3)
        print(format_departures(deps, cfg['stop_name'], cfg.get('walk_minutes', 7)))

    elif "--test-arrival" in sys.argv:
        # Test departures-by-arrival for a given target time
        cfg = load_ptv_config()
        target_str = sys.argv[sys.argv.index("--test-arrival") + 1]
        target = datetime.now(tz=MELB_TZ).replace(
            hour=int(target_str.split(":")[0]),
            minute=int(target_str.split(":")[1]),
            second=0, microsecond=0
        )
        deps = get_departures_by_arrival(
            cfg['api_key'], cfg['stop_id'], cfg['stop_name'],
            cfg['cache_file'], target, n=3
        )
        print(format_departures(deps, cfg['stop_name'], cfg.get('walk_minutes', 7)))

    elif "--find-stop" in sys.argv:
        # Search the GTFS stops list for a station name — useful for finding stop_id values
        import zipfile, io, csv, requests
        cfg   = load_ptv_config()
        query = sys.argv[sys.argv.index("--find-stop") + 1]
        print(f"Downloading GTFS to search for '{query}'... (this takes ~30s)")
        outer = zipfile.ZipFile(io.BytesIO(requests.get(GTFS_URL, timeout=180).content))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(f"{cfg['gtfs_zip_folder']}/google_transit.zip")))
        with inner.open("stops.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                if query.lower() in row["stop_name"].lower():
                    print(f"  {row['stop_id']}  {row['stop_name']}  {row.get('platform_code', '')}  {row.get('stop_desc', '')}")

    else:
        print(__doc__)