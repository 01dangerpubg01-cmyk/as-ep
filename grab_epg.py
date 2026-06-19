#!/usr/bin/env python3
"""
Astro Malaysia EPG Grabber
Source: content.astro.com.my (via contenthub-api.eco.astro.com.my)

- Channels API : https://contenthub-api.eco.astro.com.my/channel/all.json
- Schedule API : https://contenthub-api.eco.astro.com.my/channel/{id}.json
- Details API  : https://contenthub-api.eco.astro.com.my/api/v1/linear-detail?siTrafficKey={key}

Archive logic:
  - Past  : keep last 14 days only (older entries removed)
  - Future: fetch all days the API provides (no hard cap)
             stops when 2 consecutive empty days are seen
"""

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import (
    Element, SubElement, ElementTree,
    parse as et_parse, indent,
)

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("astro-epg")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_API      = "https://contenthub-api.eco.astro.com.my"
ALL_CHANNELS  = f"{BASE_API}/channel/all.json"
CHANNEL_SCHED = f"{BASE_API}/channel/{{site_id}}.json"
PROG_DETAIL   = f"{BASE_API}/api/v1/linear-detail"

TIMEZONE        = timezone(timedelta(hours=8))   # MYT / UTC+8
DATE_FMT_XMLTV  = "%Y%m%d%H%M%S %z"
DATE_FMT_DAY    = "%Y-%m-%d"
KEEP_PAST_DAYS  = 14   # remove archive older than this

HEADERS = {
    "Accept":       "application/json",
    "User-Agent":   (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def get_json(url: str, params: dict = None, retries: int = 3) -> dict | list | None:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            log.warning("HTTP %s — %s (attempt %d/%d)", e.response.status_code, url, attempt, retries)
            if e.response.status_code in (403, 404):
                break
        except Exception as e:
            log.warning("Error fetching %s — %s (attempt %d/%d)", url, e, attempt, retries)
        if attempt < retries:
            time.sleep(2 * attempt)
    return None

# ---------------------------------------------------------------------------
# DateTime helpers
# ---------------------------------------------------------------------------

def utc_str_to_myt(dt_str: str) -> datetime | None:
    """Parse UTC datetime string and return MYT-aware datetime."""
    if not dt_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S",  "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(dt_str[:len(fmt)], fmt)
            return dt.replace(tzinfo=timezone.utc).astimezone(TIMEZONE)
        except ValueError:
            continue
    return None


def parse_xmltv_dt(dt_str: str) -> datetime | None:
    """Parse XMLTV datetime string back to aware datetime."""
    if not dt_str:
        return None
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(dt_str.strip()[:len(fmt)], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=TIMEZONE)
        except ValueError:
            continue
    return None


def parse_duration(dur: str) -> int:
    """'HH:MM:SS' → seconds."""
    try:
        h, m, s = (int(x) for x in dur.split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return 3600

# ---------------------------------------------------------------------------
# Fetch channels
# ---------------------------------------------------------------------------

def fetch_all_channels(channel_ids: list[int] | None = None) -> list[dict]:
    log.info("Fetching channel list from API ...")
    data = get_json(ALL_CHANNELS)
    if not data:
        log.error("Could not fetch channel list — exiting.")
        sys.exit(1)

    channels = data.get("response", []) if isinstance(data, dict) else data
    log.info("API returned %d channels.", len(channels))

    if channel_ids:
        channels = [c for c in channels if c.get("id") in channel_ids]
        log.info("Filtered to %d channels by config.", len(channels))

    return channels

# ---------------------------------------------------------------------------
# Fetch schedule for one channel
# ---------------------------------------------------------------------------

def fetch_schedule(site_id) -> dict:
    """Return full schedule JSON for a channel (keyed by date string)."""
    url  = CHANNEL_SCHED.format(site_id=site_id)
    data = get_json(url)
    if not data:
        return {}
    resp = data.get("response", {}) if isinstance(data, dict) else {}
    return resp.get("schedule", {})


def fetch_program_details(si_traffic_key: str) -> dict:
    """Fetch extended programme details (synopsis, cast, etc.)."""
    if not si_traffic_key:
        return {}
    data = get_json(PROG_DETAIL, params={"siTrafficKey": si_traffic_key})
    if not data:
        return {}
    return data.get("response", {}) if isinstance(data, dict) else {}

# ---------------------------------------------------------------------------
# Collect events for a channel — probe until API runs out of data
# ---------------------------------------------------------------------------

def collect_events(channel: dict, today: datetime) -> tuple[str, list[dict]]:
    """
    Fetch schedule from today forwards.
    Stops when 2 consecutive days have no data.
    Returns (site_id, list_of_event_dicts).
    """
    site_id  = str(channel.get("id", ""))
    ch_name  = (channel.get("title") or site_id)[:35]
    schedule = fetch_schedule(site_id)

    if not schedule:
        log.info("  %-35s  (no schedule data)", ch_name)
        return site_id, []

    events       = []
    empty_streak = 0
    day          = 0

    while True:
        date_key = (today + timedelta(days=day)).strftime(DATE_FMT_DAY)
        items    = schedule.get(date_key, [])

        if items:
            events.extend(items)
            empty_streak = 0
            log.info("  %-35s  %s  →  %d items", ch_name, date_key, len(items))
        else:
            empty_streak += 1
            log.info("  %-35s  %s  →  (no data)", ch_name, date_key)
            if empty_streak >= 2:
                break

        day += 1

    return site_id, events

# ---------------------------------------------------------------------------
# Load and trim existing XMLTV
# ---------------------------------------------------------------------------

def load_existing_xml(path: Path, keep_past_days: int):
    """
    Parse existing XML.
    Drop programmes older than keep_past_days.
    Returns (channels_map, programmes_map).
    """
    ch_map   = {}
    prog_map = {}

    if not path.exists():
        log.info("No existing XML file — starting fresh.")
        return ch_map, prog_map

    try:
        root = et_parse(str(path)).getroot()
    except Exception as e:
        log.warning("Could not parse existing XML (%s) — starting fresh.", e)
        return ch_map, prog_map

    cutoff = datetime.now(tz=TIMEZONE) - timedelta(days=keep_past_days)
    kept = dropped = 0

    for el in root.findall("channel"):
        ch_map[el.get("id", "")] = el

    for el in root.findall("programme"):
        dt = parse_xmltv_dt(el.get("start", ""))
        if dt and dt < cutoff:
            dropped += 1
            continue
        ch_id = el.get("channel", "")
        prog_map.setdefault(ch_id, []).append(el)
        kept += 1

    log.info(
        "Loaded existing XML — %d channels | %d programmes kept | %d expired (>%d days) dropped.",
        len(ch_map), kept, dropped, keep_past_days,
    )
    return ch_map, prog_map

# ---------------------------------------------------------------------------
# Build <channel> element
# ---------------------------------------------------------------------------

def build_channel_el(ch: dict) -> Element:
    site_id  = str(ch.get("id", ""))
    name     = ch.get("title") or site_id
    number   = str(ch.get("channelNumber") or ch.get("number") or "")
    logo     = ch.get("logoUrl") or ch.get("imageUrl") or ""

    el = Element("channel", id=site_id)
    SubElement(el, "display-name", lang="en").text = name
    if number:
        SubElement(el, "display-name").text = number
    if logo:
        SubElement(el, "icon", src=logo)
    SubElement(el, "url").text = f"https://content.astro.com.my/channels/{number}"
    return el

# ---------------------------------------------------------------------------
# Build <programme> element from one schedule item
# ---------------------------------------------------------------------------

def build_programme_el(item: dict, ch_id: str, fetch_details: bool = True) -> Element | None:
    si_key   = item.get("siTrafficKey", "")
    details  = fetch_program_details(si_key) if (fetch_details and si_key) else {}

    title    = details.get("title") or item.get("title") or "Unknown"
    desc     = details.get("longSynopsis") or details.get("shortSynopsis") or ""
    subtitle = item.get("subtitles") or ""
    image    = details.get("imageUrl") or ""
    cert     = details.get("certification") or ""
    cast_str = details.get("cast") or ""
    dir_str  = details.get("director") or ""

    # Parse start time from UTC
    start_dt = utc_str_to_myt(item.get("datetimeInUtc") or "")
    if not start_dt:
        return None

    dur_secs = parse_duration(item.get("duration") or "")
    stop_dt  = start_dt + timedelta(seconds=dur_secs)

    # Episode / season from title patterns
    ep_num = None
    sn_num = None
    import re
    if m := re.search(r"Ep(\d+)$", item.get("title") or ""):
        ep_num = int(m.group(1))
    if m := re.search(r" S(\d+)", title):
        sn_num = int(m.group(1))

    prog = Element(
        "programme",
        start=start_dt.strftime(DATE_FMT_XMLTV),
        stop=stop_dt.strftime(DATE_FMT_XMLTV),
        channel=ch_id,
    )

    SubElement(prog, "title", lang="en").text = title

    if subtitle:
        SubElement(prog, "sub-title", lang="en").text = subtitle

    if desc:
        SubElement(prog, "desc", lang="en").text = desc

    # Categories from subFilter
    sub_filters = details.get("subFilter", [])
    GENRE_MAP = {
        "filter/2": "Action", "filter/4": "Anime", "filter/12": "Cartoons",
        "filter/16": "Comedy", "filter/19": "Crime", "filter/24": "Drama",
        "filter/25": "Educational", "filter/36": "Horror", "filter/39": "Live Action",
        "filter/55": "Pre-school", "filter/56": "Reality", "filter/60": "Romance",
        "filter/68": "Talk Show", "filter/69": "Thriller", "filter/72": "Variety",
        "filter/75": "Series", "filter/100": "Others",
    }
    if isinstance(sub_filters, list):
        for sf in sub_filters:
            genre = GENRE_MAP.get(str(sf).lower())
            if genre:
                SubElement(prog, "category", lang="en").text = genre

    # Episode info
    if sn_num is not None or ep_num is not None:
        ep_el = SubElement(prog, "episode-num", system="xmltv_ns")
        s = str(sn_num - 1) if sn_num else ""
        e = str(ep_num - 1) if ep_num else ""
        ep_el.text = f"{s}.{e}."

    if image:
        SubElement(prog, "icon", src=image)

    if cert:
        r_el = SubElement(prog, "rating", system="LPF")
        SubElement(r_el, "value").text = cert

    # Credits
    actors    = [a.strip() for a in cast_str.split(",") if a.strip()] if cast_str else []
    directors = [d.strip() for d in dir_str.split(",") if d.strip()] if dir_str else []
    if actors or directors:
        cr = SubElement(prog, "credits")
        for d in directors:
            SubElement(cr, "director").text = d
        for a in actors:
            SubElement(cr, "actor").text = a

    return prog

# ---------------------------------------------------------------------------
# Merge and write final XMLTV
# ---------------------------------------------------------------------------

def merge_and_write(
    out_path: Path,
    api_channels: list[dict],
    new_events: dict[str, list[dict]],
    old_ch_map: dict,
    old_prog_map: dict,
    no_details: bool = False,
) -> None:
    root = Element("tv")
    root.set("generator-info-name", "astro-epg-grabber")
    root.set("source-info-name",    "Astro Malaysia")
    root.set("source-info-url",     "https://content.astro.com.my/channels")

    # --- Channels ---
    seen_ids = set()
    for ch in api_channels:
        root.append(build_channel_el(ch))
        seen_ids.add(str(ch.get("id", "")))
    for ch_id, el in old_ch_map.items():
        if ch_id not in seen_ids:
            root.append(el)   # preserve channels no longer in API

    # --- Programmes ---
    # Keyed by (channel_id, start_string) for dedup
    merged: dict[tuple, Element] = {}

    # 1. Old archive (already past-trimmed)
    for ch_id, progs in old_prog_map.items():
        for p in progs:
            merged[(ch_id, p.get("start", ""))] = p

    # 2. New data from API (overwrites same timeslot)
    new_added = replaced = 0
    for ch in api_channels:
        ch_id  = str(ch.get("id", ""))
        items  = new_events.get(ch_id, [])
        for item in items:
            el = build_programme_el(item, ch_id, fetch_details=not no_details)
            if el is None:
                continue
            key = (ch_id, el.get("start", ""))
            if key in merged:
                replaced += 1
            else:
                new_added += 1
            merged[key] = el

    for _, el in sorted(merged.items()):
        root.append(el)

    total    = len(merged)
    archived = total - new_added - replaced
    log.info(
        "Merge complete — %d total | %d new | %d updated | %d from archive.",
        total, new_added, replaced, archived,
    )

    indent(root, space="  ")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(root).write(str(out_path), encoding="utf-8", xml_declaration=True)
    log.info("Saved to %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Astro Malaysia EPG grabber — past 14 days kept, future unlimited",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Archive policy
  Past  : keep last KEEP_DAYS days (older entries removed)
  Future: probe day-by-day until API has no more data (no hard limit)

Examples
  python grab_epg.py
  python grab_epg.py --config config/channels.json --workers 10
  python grab_epg.py --channels 101 121 151
  python grab_epg.py --list-channels
  python grab_epg.py --no-details        (skip per-programme detail calls, faster)
        """,
    )
    p.add_argument("-o", "--output",     default="astro_epg.xml",
                   help="Output XMLTV file (default: astro_epg.xml)")
    p.add_argument("-w", "--workers",    type=int, default=5,
                   help="Parallel threads for schedule fetch (default: 5)")
    p.add_argument("-c", "--channels",   nargs="*", type=int,
                   help="Channel IDs to include (default: all)")
    p.add_argument("--config",           help="JSON config with channel IDs list")
    p.add_argument("--keep-days",        type=int, default=KEEP_PAST_DAYS,
                   help=f"Days of past EPG to keep (default: {KEEP_PAST_DAYS})")
    p.add_argument("--no-details",       action="store_true",
                   help="Skip per-programme detail API calls (faster, less metadata)")
    p.add_argument("--list-channels",    action="store_true",
                   help="Print all channels as JSON and exit")
    return p.parse_args()


def main():
    args = parse_args()

    # Load channel filter
    channel_ids = args.channels or []
    if args.config and not channel_ids:
        cfg_path = Path(args.config)
        if cfg_path.exists():
            lines = [l for l in cfg_path.read_text("utf-8").splitlines()
                     if not l.strip().startswith("//")]
            cfg = json.loads("\n".join(lines))
            channel_ids = [c for c in cfg.get("channels", []) if isinstance(c, int)]

    api_channels = fetch_all_channels(channel_ids or None)

    if args.list_channels:
        print(json.dumps(
            [{"id": c.get("id"), "name": c.get("title"), "number": c.get("channelNumber")}
             for c in api_channels],
            indent=2, ensure_ascii=False,
        ))
        return

    if not api_channels:
        log.error("No channels found. Exiting.")
        sys.exit(1)

    out_path = Path(args.output)

    # Step 1 — load + trim existing XML
    old_ch_map, old_prog_map = load_existing_xml(out_path, args.keep_days)

    # Step 2 — fetch today onwards, no day limit
    today = datetime.now(tz=TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    log.info(
        "Fetching schedule from %s onwards for %d channel(s) with %d workers ...",
        today.strftime(DATE_FMT_DAY), len(api_channels), args.workers,
    )

    new_events: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect_events, ch, today): ch for ch in api_channels}
        for fut in as_completed(futures):
            try:
                site_id, events = fut.result()
                new_events[site_id] = events
            except Exception as e:
                ch = futures[fut]
                log.error("Failed %s — %s", ch.get("title"), e)

    total = sum(len(v) for v in new_events.values())
    log.info("Fetch complete — %d schedule items across %d channels.", total, len(new_events))

    # Step 3 — merge + write
    merge_and_write(
        out_path, api_channels, new_events,
        old_ch_map, old_prog_map,
        no_details=args.no_details,
    )


if __name__ == "__main__":
    main()
