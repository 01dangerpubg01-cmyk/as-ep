#!/usr/bin/env python3
"""
Astro Malaysia EPG Grabber
Source: content.astro.com.my (via contenthub-api.eco.astro.com.my)

Endpoints (verified from iptv-org/epg source):
  - All channels : https://contenthub-api.eco.astro.com.my/channel/all.json
  - Schedule     : https://contenthub-api.eco.astro.com.my/channel/{id}.json
                   Response: { response: { schedule: { "YYYY-MM-DD": [ items ] } } }
  - Details      : https://contenthub-api.eco.astro.com.my/api/v1/linear-detail?siTrafficKey=X

Schedule item keys (from iptv-org source):
  datetimeInUtc, duration, title, subtitles, siTrafficKey

Archive logic:
  - Past  : keep last 14 days
  - Future: probe day by day until 2 consecutive empty days
"""

import argparse
import json
import logging
import re
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

TIMEZONE       = timezone(timedelta(hours=8))   # MYT / UTC+8
DATE_FMT_XMLTV = "%Y%m%d%H%M%S %z"
DATE_FMT_DAY   = "%Y-%m-%d"
KEEP_PAST_DAYS = 14

HEADERS = {
    "Accept":     "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def get_json(url: str, params: dict = None, retries: int = 3):
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
            log.warning("Error %s — %s (attempt %d/%d)", url, e, attempt, retries)
        if attempt < retries:
            time.sleep(2 * attempt)
    return None

# ---------------------------------------------------------------------------
# DateTime
# ---------------------------------------------------------------------------

def utc_to_myt(dt_str: str):
    """UTC string from API → MYT aware datetime."""
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


def parse_xmltv_dt(dt_str: str):
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
        parts = [int(x) for x in dur.split(":")]
        return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
    except Exception:
        return 1800   # default 30 min

# ---------------------------------------------------------------------------
# Fetch channels
# ---------------------------------------------------------------------------

def fetch_all_channels(channel_ids: list = None) -> list:
    log.info("Fetching channel list ...")
    data = get_json(ALL_CHANNELS)
    if not data:
        log.error("Cannot fetch channels — exiting.")
        sys.exit(1)

    channels = data.get("response", []) if isinstance(data, dict) else data
    log.info("API returned %d channels.", len(channels))

    if channel_ids:
        channels = [c for c in channels if c.get("id") in channel_ids]
        log.info("Filtered to %d channels.", len(channels))

    return channels

# ---------------------------------------------------------------------------
# Fetch schedule
# ---------------------------------------------------------------------------

def fetch_schedule(site_id) -> dict:
    """
    Returns schedule dict keyed by date: { "YYYY-MM-DD": [ items... ] }
    Each item has: datetimeInUtc, duration, title, subtitles, siTrafficKey, ...
    """
    url  = CHANNEL_SCHED.format(site_id=site_id)
    data = get_json(url)
    if not data:
        return {}

    # Response structure: { "response": { "schedule": { "2026-06-19": [...] } } }
    resp     = data.get("response", {}) if isinstance(data, dict) else {}
    schedule = resp.get("schedule", {})

    # Fallback: sometimes schedule is at top level
    if not schedule and isinstance(data, dict):
        schedule = data.get("schedule", {})

    return schedule if isinstance(schedule, dict) else {}


def fetch_details(si_key: str) -> dict:
    if not si_key:
        return {}
    data = get_json(PROG_DETAIL, params={"siTrafficKey": si_key})
    if not data:
        return {}
    return data.get("response", {}) if isinstance(data, dict) else {}

# ---------------------------------------------------------------------------
# Collect events (probe future until empty)
# ---------------------------------------------------------------------------

def collect_events(channel: dict, today: datetime) -> tuple:
    site_id  = str(channel.get("id", ""))
    ch_name  = (channel.get("title") or site_id)[:35]
    schedule = fetch_schedule(site_id)

    if not schedule:
        log.info("  %-35s  no schedule returned", ch_name)
        return site_id, []

    all_items    = []
    empty_streak = 0
    day          = 0

    while True:
        date_key = (today + timedelta(days=day)).strftime(DATE_FMT_DAY)
        items    = schedule.get(date_key, [])

        if items:
            all_items.extend(items)
            empty_streak = 0
            log.info("  %-35s  %s  →  %d items", ch_name, date_key, len(items))
        else:
            empty_streak += 1
            if empty_streak >= 2:
                break

        day += 1

    return site_id, all_items

# ---------------------------------------------------------------------------
# Build programme element
# ---------------------------------------------------------------------------

def build_programme_el(item: dict, ch_id: str, with_details: bool) -> Element | None:
    """
    item fields from schedule API:
      title          - programme title
      datetimeInUtc  - UTC start time
      duration       - "HH:MM:SS"
      subtitles      - episode subtitle
      siTrafficKey   - key for detail API call
      shortSynopsis  - sometimes present directly in schedule item
      longSynopsis   - sometimes present directly in schedule item
    """
    # --- Start time ---
    start_dt = utc_to_myt(item.get("datetimeInUtc", ""))
    if not start_dt:
        return None

    # --- Duration / stop ---
    stop_dt = start_dt + timedelta(seconds=parse_duration(item.get("duration", "")))

    # --- Title: directly in schedule item ---
    title = (
        item.get("programmeTitle") or
        item.get("title") or
        item.get("programmeName") or
        item.get("name") or
        "Unknown"
    )

    # --- Description: try from schedule item first ---
    desc = (
        item.get("longSynopsis") or
        item.get("shortSynopsis") or
        item.get("synopsis") or
        item.get("description") or
        ""
    )

    # --- Optional: fetch richer details ---
    details = {}
    if with_details and item.get("siTrafficKey"):
        details = fetch_details(item["siTrafficKey"])

    if details:
        title = details.get("title") or title
        desc  = details.get("longSynopsis") or details.get("shortSynopsis") or desc

    subtitle = item.get("subtitles") or item.get("episodeTitle") or ""
    image    = details.get("imageUrl") or item.get("imageUrl") or item.get("thumbnail") or ""
    cert     = details.get("certification") or item.get("certification") or ""
    cast_str = details.get("cast") or item.get("cast") or ""
    dir_str  = details.get("director") or item.get("director") or ""

    # --- Genre from subFilter ---
    GENRE_MAP = {
        "filter/2": "Action", "filter/4": "Anime", "filter/12": "Cartoons",
        "filter/16": "Comedy", "filter/19": "Crime", "filter/24": "Drama",
        "filter/25": "Educational", "filter/36": "Horror", "filter/39": "Live Action",
        "filter/55": "Pre-school", "filter/56": "Reality", "filter/60": "Romance",
        "filter/68": "Talk Show", "filter/69": "Thriller", "filter/72": "Variety",
        "filter/75": "Series", "filter/100": "Others",
    }
    sub_filters = details.get("subFilter") or item.get("subFilter") or []
    genres = []
    if isinstance(sub_filters, list):
        genres = [GENRE_MAP[sf.lower()] for sf in sub_filters if sf.lower() in GENRE_MAP]

    # Fallback genre from item
    if not genres and item.get("genre"):
        genres = [item["genre"]]

    # --- Episode / Season ---
    ep_num = item.get("episodeNumber") or details.get("episodeNumber")
    sn_num = item.get("seasonNumber")  or details.get("seasonNumber")
    if not ep_num:
        m = re.search(r"Ep(\d+)$", item.get("title") or "")
        if m:
            ep_num = int(m.group(1))
    if not sn_num:
        m = re.search(r" S(\d+)", title)
        if m:
            sn_num = int(m.group(1))

    # --- Build element ---
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

    for g in genres:
        SubElement(prog, "category", lang="en").text = g

    if sn_num or ep_num:
        ep_el = SubElement(prog, "episode-num", system="xmltv_ns")
        s = str(int(sn_num) - 1) if sn_num else ""
        e = str(int(ep_num) - 1) if ep_num else ""
        ep_el.text = f"{s}.{e}."

    if image:
        SubElement(prog, "icon", src=image)

    if cert:
        r_el = SubElement(prog, "rating", system="LPF")
        SubElement(r_el, "value").text = cert

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
# Build channel element
# ---------------------------------------------------------------------------

def build_channel_el(ch: dict) -> Element:
    site_id = str(ch.get("id", ""))
    name    = ch.get("title") or site_id
    number  = str(ch.get("channelNumber") or ch.get("number") or "")
    logo    = ch.get("logoUrl") or ch.get("imageUrl") or ""

    el = Element("channel", id=site_id)
    SubElement(el, "display-name", lang="en").text = name
    if number:
        SubElement(el, "display-name").text = number
    if logo:
        SubElement(el, "icon", src=logo)
    SubElement(el, "url").text = f"https://content.astro.com.my/channels/{number}"
    return el

# ---------------------------------------------------------------------------
# Load + trim existing XML
# ---------------------------------------------------------------------------

def load_existing_xml(path: Path, keep_days: int):
    ch_map   = {}
    prog_map = {}

    if not path.exists():
        log.info("No existing XML — starting fresh.")
        return ch_map, prog_map

    try:
        root = et_parse(str(path)).getroot()
    except Exception as e:
        log.warning("Cannot parse existing XML (%s) — starting fresh.", e)
        return ch_map, prog_map

    cutoff = datetime.now(tz=TIMEZONE) - timedelta(days=keep_days)
    kept = dropped = 0

    for el in root.findall("channel"):
        ch_map[el.get("id", "")] = el

    for el in root.findall("programme"):
        dt = parse_xmltv_dt(el.get("start", ""))
        if dt and dt < cutoff:
            dropped += 1
            continue
        prog_map.setdefault(el.get("channel", ""), []).append(el)
        kept += 1

    log.info(
        "Loaded XML — %d channels | %d kept | %d expired (>%d days) removed.",
        len(ch_map), kept, dropped, keep_days,
    )
    return ch_map, prog_map

# ---------------------------------------------------------------------------
# Merge + write
# ---------------------------------------------------------------------------

def merge_and_write(out_path, api_channels, new_events, old_ch_map, old_prog_map, with_details):
    root = Element("tv")
    root.set("generator-info-name", "astro-epg-grabber")
    root.set("source-info-name",    "Astro Malaysia")
    root.set("source-info-url",     "https://content.astro.com.my/channels")

    # Channels
    seen = set()
    for ch in api_channels:
        root.append(build_channel_el(ch))
        seen.add(str(ch.get("id", "")))
    for ch_id, el in old_ch_map.items():
        if ch_id not in seen:
            root.append(el)

    # Programmes — keyed by (ch_id, start) for dedup
    merged = {}

    # 1. Old archive (already trimmed)
    for ch_id, progs in old_prog_map.items():
        for p in progs:
            merged[(ch_id, p.get("start", ""))] = p

    # 2. New from API
    new_added = replaced = skipped = 0
    for ch in api_channels:
        ch_id = str(ch.get("id", ""))
        items = new_events.get(ch_id, [])
        for item in items:
            el = build_programme_el(item, ch_id, with_details)
            if el is None:
                skipped += 1
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
        "Merge — %d total programmes | %d new | %d updated | %d archive | %d skipped (no time).",
        total, new_added, replaced, archived, skipped,
    )

    indent(root, space="  ")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(root).write(str(out_path), encoding="utf-8", xml_declaration=True)
    log.info("Saved %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Astro EPG grabber — past 14 days kept, future unlimited",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python grab_epg.py
  python grab_epg.py --config config/channels.json --workers 10
  python grab_epg.py --channels 397 167 358 --workers 5
  python grab_epg.py --list-channels
  python grab_epg.py --no-details    (faster, skips per-programme detail API)
        """,
    )
    p.add_argument("-o", "--output",    default="astro_epg.xml")
    p.add_argument("-w", "--workers",   type=int, default=5)
    p.add_argument("-c", "--channels",  nargs="*", type=int)
    p.add_argument("--config")
    p.add_argument("--keep-days",       type=int, default=KEEP_PAST_DAYS)
    p.add_argument("--no-details",      action="store_true",
                   help="Skip per-programme detail API calls (faster, basic title+time only)")
    p.add_argument("--list-channels",   action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    # Load channel filter from config
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
        log.error("No channels found — exiting.")
        sys.exit(1)

    out_path = Path(args.output)

    # Step 1: Load + trim existing XML
    old_ch_map, old_prog_map = load_existing_xml(out_path, args.keep_days)

    # Step 2: Fetch schedule from today onwards
    today = datetime.now(tz=TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    log.info(
        "Fetching from %s for %d channel(s) with %d workers%s ...",
        today.strftime(DATE_FMT_DAY), len(api_channels), args.workers,
        " [no-details mode]" if args.no_details else " [with details]",
    )

    new_events = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect_events, ch, today): ch for ch in api_channels}
        for fut in as_completed(futures):
            try:
                site_id, items = fut.result()
                new_events[site_id] = items
            except Exception as e:
                ch = futures[fut]
                log.error("Failed %s — %s", ch.get("title"), e)

    total = sum(len(v) for v in new_events.values())
    log.info("Schedule fetch done — %d total items.", total)

    # Step 3: Merge + write
    merge_and_write(
        out_path, api_channels, new_events,
        old_ch_map, old_prog_map,
        with_details=not args.no_details,
    )


if __name__ == "__main__":
    main()
