#!/usr/bin/env python3
"""
Astro Malaysia EPG Grabber
---------------------------
Logic:
  - Past  : 14 நாளுக்கு பழையது delete, மீதி keep
  - Future : API எவ்வளவு நாள் data தருகிறதோ அனைத்தும் fetch
             (2 consecutive empty days வரும் வரை தொடரும் — no hard limit)
  - தினமும் run → XML accumulate ஆகும், overwrite ஆகாது
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

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("astro-epg")

# ─── Constants ────────────────────────────────────────────────────────────────
CHANNELS_ENDPOINTS = [
    "http://ams-api.astro.com.my/ams/v3/getChannels",
    "https://ams-api.astro.com.my/ams/v3/getChannels",
    "https://contenthub-api.eco.astro.com.my/channel/v1/",
]
EVENTS_ENDPOINTS = [
    "http://ams-api.astro.com.my/ams/v3/getEvents",
    "https://ams-api.astro.com.my/ams/v3/getEvents",
]

TIMEZONE       = timezone(timedelta(hours=8))   # MYT (UTC+8)
DATE_FMT_API   = "%Y-%m-%d"
DATE_FMT_XMLTV = "%Y%m%d%H%M%S %z"
KEEP_PAST_DAYS = 14   # past இந்த நாளுக்கு முன்பான data delete ஆகும்

HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://content.astro.com.my/",
    "Origin":          "https://content.astro.com.my",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def safe_get(url: str, params: dict = None, retries: int = 3, delay: float = 2.0):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if resp.status_code == 403:
                log.warning("403 Forbidden — %s", url)
                break
            resp.raise_for_status()
            return resp.json()
        except requests.JSONDecodeError:
            return None
        except requests.RequestException as exc:
            log.warning("Attempt %d/%d — %s : %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(delay * attempt)
    return None


def try_endpoints(endpoints: list, params: dict = None):
    for url in endpoints:
        result = safe_get(url, params=params)
        if result is not None:
            return result
    return None

# ─── DateTime helpers ─────────────────────────────────────────────────────────

def parse_datetime_api(dt_str: str):
    if not dt_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S",    "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(dt_str[:len(fmt)], fmt).replace(tzinfo=TIMEZONE)
        except ValueError:
            continue
    return None


def parse_datetime_xmltv(dt_str: str):
    if not dt_str:
        return None
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(dt_str.strip()[:len(fmt)], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=TIMEZONE)
        except ValueError:
            continue
    return None


def duration_to_seconds(dur_str: str) -> int:
    try:
        parts = [int(x) for x in dur_str.split(":")]
        return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
    except Exception:
        return 3600

# ─── Load existing XMLTV (trim past > 14 days) ───────────────────────────────

def load_existing_xml(path: Path):
    """
    Existing XML parse → past 14 நாளுக்கு முன்பான programmes drop.
    Returns (channels_map, programmes_map).
    """
    channels_map   = {}
    programmes_map = {}

    if not path.exists():
        log.info("No existing XML — starting fresh.")
        return channels_map, programmes_map

    try:
        root = et_parse(str(path)).getroot()
    except Exception as exc:
        log.warning("Could not parse existing XML (%s) — starting fresh.", exc)
        return channels_map, programmes_map

    cutoff = datetime.now(tz=TIMEZONE) - timedelta(days=KEEP_PAST_DAYS)

    for ch in root.findall("channel"):
        channels_map[ch.get("id", "")] = ch

    kept = expired = 0
    for prog in root.findall("programme"):
        start_dt = parse_datetime_xmltv(prog.get("start", ""))
        if start_dt and start_dt < cutoff:
            expired += 1
            continue
        ch_id = prog.get("channel", "")
        programmes_map.setdefault(ch_id, []).append(prog)
        kept += 1

    log.info(
        "Existing XML → %d channels | %d programmes kept | %d expired (>%d days) removed.",
        len(channels_map), kept, expired, KEEP_PAST_DAYS,
    )
    return channels_map, programmes_map

# ─── Fetch channels ───────────────────────────────────────────────────────────

def fetch_channels(channel_ids: list = None) -> list:
    log.info("Fetching channel list from Astro API …")
    data = try_endpoints(CHANNELS_ENDPOINTS, params={"lang": "en"})
    if not data:
        log.error("Could not fetch channels.")
        sys.exit(1)

    channels = (data if isinstance(data, list) else
                data.get("channels") or data.get("response") or data.get("data") or [])

    if channel_ids:
        channels = [c for c in channels if c.get("channelId") in channel_ids]

    log.info("API returned %d channel(s).", len(channels))
    return channels

# ─── Fetch events — probe until API has no more future data ──────────────────

def fetch_events_for_day(channel_id: int, date: datetime) -> list:
    date_str = date.strftime(DATE_FMT_API)
    data = try_endpoints(EVENTS_ENDPOINTS, params={
        "periodStart": f"{date_str} 00:00:00",
        "periodEnd":   f"{date_str} 23:59:59",
        "channelId":   channel_id,
    })
    if not data:
        return []
    return (data if isinstance(data, list) else
            data.get("events") or data.get("response") or data.get("data") or [])


def collect_events(channel: dict, today: datetime) -> tuple:
    """
    Today முதல் day-by-day fetch.
    Future limit இல்லை — API 2 consecutive empty days தரும் வரை தொடரும்.
    """
    ch_id        = channel.get("channelId")
    ch_name      = (channel.get("channelTitle") or str(ch_id))[:30]
    all_events   = []
    empty_streak = 0
    day          = 0

    while True:
        date = today + timedelta(days=day)
        evs  = fetch_events_for_day(ch_id, date)

        if evs:
            all_events.extend(evs)
            empty_streak = 0
            log.info("  %-30s  %s  →  %d events", ch_name, date.strftime("%Y-%m-%d"), len(evs))
        else:
            empty_streak += 1
            log.info("  %-30s  %s  →  (no data)", ch_name, date.strftime("%Y-%m-%d"))
            # 2 consecutive empty days = API exhausted for this channel
            if empty_streak >= 2:
                break

        day += 1

    return ch_id, all_events

# ─── Build <programme> element ────────────────────────────────────────────────

def event_to_element(ev: dict, ch_id: str):
    title    = ev.get("programmeTitle") or ev.get("title") or "Unknown"
    desc     = ev.get("shortSynopsis") or ev.get("synopsis") or ev.get("description") or ""
    genre    = ev.get("genre")         or ev.get("category") or ""
    subgenre = ev.get("subGenre")      or ""
    episode  = ev.get("episodeNumber") or ""
    season   = ev.get("seasonNumber")  or ""
    year     = ev.get("productionYear") or ""
    rating   = ev.get("rating")        or ""
    actors   = ev.get("actors")        or ""
    director = ev.get("directors")     or ""
    icon     = ev.get("epgEventImage") or ev.get("thumbnailUrl") or ""

    start_dt = parse_datetime_api(ev.get("displayDateTime") or ev.get("startTime") or "")
    if not start_dt:
        return None

    dur_str = ev.get("displayDuration") or ""
    stop_dt = start_dt + timedelta(seconds=duration_to_seconds(dur_str) if dur_str else 3600)

    prog = Element("programme",
                   start=start_dt.strftime(DATE_FMT_XMLTV),
                   stop=stop_dt.strftime(DATE_FMT_XMLTV),
                   channel=ch_id)

    SubElement(prog, "title", lang="en").text = title
    if desc:     SubElement(prog, "desc",     lang="en").text = desc
    if genre:    SubElement(prog, "category", lang="en").text = genre
    if subgenre and subgenre != genre:
                 SubElement(prog, "category", lang="en").text = subgenre
    if season or episode:
        ep = SubElement(prog, "episode-num", system="xmltv_ns")
        ep.text = f"{int(season)-1 if season else ''}.{int(episode)-1 if episode else ''}."
    if icon:     SubElement(prog, "icon", src=icon)
    if year:     SubElement(prog, "date").text = str(year)
    if rating:
        re = SubElement(prog, "rating")
        SubElement(re, "value").text = rating
    if actors or director:
        cr = SubElement(prog, "credits")
        for d in (director if isinstance(director, list) else director.split(",") if director else []):
            if d.strip(): SubElement(cr, "director").text = d.strip()
        for a in (actors if isinstance(actors, list) else actors.split(",") if actors else []):
            if a.strip(): SubElement(cr, "actor").text = a.strip()

    return prog


def channel_to_element(ch: dict) -> Element:
    ch_id  = str(ch.get("channelId", ""))
    name   = ch.get("channelTitle") or ch.get("title") or ch.get("name") or ch_id
    num    = str(ch.get("channelNumber") or ch.get("number") or "")
    cat    = ch.get("category") or ch.get("genre") or ""
    logo   = ch.get("channelLogoPath") or ch.get("logoUrl") or ""

    el = Element("channel", id=ch_id)
    SubElement(el, "display-name", lang="en").text = name
    if num:  SubElement(el, "display-name").text = num
    if logo: SubElement(el, "icon", src=logo)
    if cat:  SubElement(el, "category", lang="en").text = cat
    SubElement(el, "url").text = f"https://content.astro.com.my/channels/{num}"
    return el

# ─── Merge & write ────────────────────────────────────────────────────────────

def merge_and_write(out_path, api_channels, api_events, old_channels, old_programmes):
    root = Element("tv")
    root.set("generator-info-name", "astro-epg-grabber")
    root.set("generator-info-url",  "https://github.com/YOUR_USERNAME/astro-epg-grabber")
    root.set("source-info-name",    "Astro Malaysia")
    root.set("source-info-url",     "https://content.astro.com.my/channels")

    # Channels — API data wins; old-only channels kept
    seen = set()
    for ch in api_channels:
        root.append(channel_to_element(ch))
        seen.add(str(ch.get("channelId", "")))
    for ch_id, el in old_channels.items():
        if ch_id not in seen:
            root.append(el)

    # Programmes — (channel_id, start) keyed map for dedup
    prog_map = {}

    # 1. Old archive (already trimmed to 14 days)
    for ch_id, progs in old_programmes.items():
        for p in progs:
            prog_map[(ch_id, p.get("start", ""))] = p

    # 2. New API data — overwrites same timeslot
    new_added = replaced = 0
    for ch in api_channels:
        ch_id  = str(ch.get("channelId", ""))
        events = api_events.get(ch.get("channelId"), [])
        for ev in events:
            el = event_to_element(ev, ch_id)
            if el is None:
                continue
            key = (ch_id, el.get("start", ""))
            if key in prog_map:
                replaced += 1
            else:
                new_added += 1
            prog_map[key] = el

    # 3. Sort by (channel, start) and append
    for _, el in sorted(prog_map.items()):
        root.append(el)

    total = len(prog_map)
    archived = total - new_added - replaced
    log.info(
        "Merge → %d total programmes | %d new | %d updated | %d from archive.",
        total, new_added, replaced, archived,
    )

    indent(root, space="  ")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(root).write(str(out_path), encoding="utf-8", xml_declaration=True)
    log.info("✓ Saved → %s  (%.1f KB)", out_path, out_path.stat().st_size / 1024)

# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Astro EPG grabber — past 14 days kept, future unlimited",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Past  : 14 நாளுக்கு பழையது delete, மீதி keep
Future: API எவ்வளவு நாள் data தருகிறதோ அனைத்தும் fetch (no limit)

Examples:
  python grab_epg.py
  python grab_epg.py --config config/channels.json --workers 10
  python grab_epg.py --channels 121 151 301
  python grab_epg.py --list-channels
        """,
    )
    p.add_argument("-o", "--output",      default="astro_epg.xml")
    p.add_argument("-w", "--workers",     type=int, default=5)
    p.add_argument("-c", "--channels",    nargs="*", type=int)
    p.add_argument("--config",            type=str)
    p.add_argument("--keep-days",         type=int, default=KEEP_PAST_DAYS,
                   help=f"Past days to keep (default: {KEEP_PAST_DAYS})")
    p.add_argument("--list-channels",     action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    global KEEP_PAST_DAYS
    KEEP_PAST_DAYS = args.keep_days

    # Channel filter
    channel_ids = args.channels
    if args.config and not channel_ids:
        cfg_path = Path(args.config)
        if cfg_path.exists():
            lines = [l for l in cfg_path.read_text("utf-8").splitlines()
                     if not l.strip().startswith("//")]
            cfg = json.loads("\n".join(lines))
            channel_ids = [c for c in cfg.get("channels", []) if isinstance(c, int)]

    api_channels = fetch_channels(channel_ids or None)

    if args.list_channels:
        print(json.dumps(
            [{"id": c.get("channelId"), "name": c.get("channelTitle"),
              "number": c.get("channelNumber"), "cat": c.get("category")}
             for c in api_channels],
            indent=2, ensure_ascii=False,
        ))
        return

    if not api_channels:
        log.error("No channels. Exiting.")
        sys.exit(1)

    out_path = Path(args.output)

    # Step 1: Load existing XML, drop >14 day old data
    old_channels, old_programmes = load_existing_xml(out_path)

    # Step 2: Fetch today onwards — no future day limit
    today = datetime.now(tz=TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    log.info(
        "Fetching from today (%s) onwards for %d channel(s) — probing until API exhausted …",
        today.strftime("%Y-%m-%d"), len(api_channels),
    )

    api_events = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect_events, ch, today): ch for ch in api_channels}
        for fut in as_completed(futures):
            try:
                ch_id, events = fut.result()
                api_events[ch_id] = events
            except Exception as exc:
                log.error("Error: %s — %s", futures[fut].get("channelTitle"), exc)

    log.info("API fetch done — %d total events.", sum(len(v) for v in api_events.values()))

    # Step 3: Merge + write
    merge_and_write(out_path, api_channels, api_events, old_channels, old_programmes)


if __name__ == "__main__":
    main()
