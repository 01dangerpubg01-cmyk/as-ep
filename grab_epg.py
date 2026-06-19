#!/usr/bin/env python3
"""
Astro Malaysia EPG Grabber
Source: contenthub-api.eco.astro.com.my

Endpoints:
  - Channels : /channel/all.json
  - Schedule : /channel/{id}.json  →  response.schedule["YYYY-MM-DD"] = [items]
  - Details  : /api/v1/linear-detail?siTrafficKey=X

Item fields (from iptv-org/epg source):
  datetimeInUtc, duration, title, subtitles, siTrafficKey

Archive:
  - Past  : keep 14 days
  - Future: probe until 2 consecutive empty days
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

MYT            = timezone(timedelta(hours=8))
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

def get_json(url, params=None, retries=3):
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
# DateTime — handle ALL possible formats Astro API might return
# ---------------------------------------------------------------------------

_SAMPLE_LOGGED = False   # log unknown format once only

def parse_start_time(raw):
    """
    Convert Astro API datetime string → MYT-aware datetime.
    Handles multiple possible formats and logs unrecognised ones.
    """
    global _SAMPLE_LOGGED
    if not raw:
        return None

    s = str(raw).strip()

    # Format 1: ISO UTC  "2026-06-19T14:00:00Z"
    # Format 2: ISO UTC ms "2026-06-19T14:00:00.000Z"
    # Format 3: space sep "2026-06-19 14:00:00"
    # Format 4: epoch integer / float
    # Format 5: already offset "+0800" aware string

    # Try epoch number first
    try:
        ts = float(s)
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(MYT)
    except (ValueError, OSError):
        pass

    # Try ISO / space formats
    clean = s.replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f+00:00",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            dt = datetime.strptime(clean[:len(fmt)], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(MYT)
        except ValueError:
            continue

    # Try Python fromisoformat (Python 3.7+)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MYT)
    except ValueError:
        pass

    # Unknown — log sample once
    if not _SAMPLE_LOGGED:
        log.warning("UNKNOWN datetime format — sample: %r  (will skip these items)", s)
        _SAMPLE_LOGGED = True
    return None


def parse_xmltv_dt(dt_str):
    if not dt_str:
        return None
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(dt_str.strip()[:len(fmt)], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=MYT)
        except ValueError:
            continue
    return None


def parse_duration(dur):
    try:
        parts = [int(x) for x in str(dur).split(":")]
        return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
    except Exception:
        return 1800

# ---------------------------------------------------------------------------
# Fetch channels
# ---------------------------------------------------------------------------

def fetch_all_channels(channel_ids=None):
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

def fetch_schedule(site_id):
    data = get_json(CHANNEL_SCHED.format(site_id=site_id))
    if not data:
        return {}
    resp = data.get("response", {}) if isinstance(data, dict) else {}
    sched = resp.get("schedule", {})
    # Some channels return schedule at top level
    if not sched and isinstance(data, dict):
        sched = data.get("schedule", {})
    return sched if isinstance(sched, dict) else {}


def fetch_details(si_key):
    if not si_key:
        return {}
    data = get_json(PROG_DETAIL, params={"siTrafficKey": si_key})
    if not data:
        return {}
    return data.get("response", {}) if isinstance(data, dict) else {}

# ---------------------------------------------------------------------------
# Collect events
# ---------------------------------------------------------------------------

def collect_events(channel, today):
    site_id  = str(channel.get("id", ""))
    ch_name  = (channel.get("title") or site_id)[:35]
    schedule = fetch_schedule(site_id)

    if not schedule:
        log.info("  %-35s  no schedule", ch_name)
        return site_id, []

    # Log first item's raw fields once per channel (to detect field names)
    first_date = list(schedule.keys())[0] if schedule else None
    if first_date:
        items_sample = schedule[first_date]
        if items_sample:
            sample = items_sample[0]
            log.info("  %-35s  SAMPLE KEYS: %s", ch_name, list(sample.keys()))
            log.info("  %-35s  datetimeInUtc=%r  duration=%r  title=%r",
                     ch_name,
                     sample.get("datetimeInUtc"),
                     sample.get("duration"),
                     sample.get("title"))

    all_items    = []
    empty_streak = 0
    day          = 0

    while True:
        date_key = (today + timedelta(days=day)).strftime(DATE_FMT_DAY)
        items    = schedule.get(date_key, [])

        if items:
            all_items.extend(items)
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= 2:
                break
        day += 1

    log.info("  %-35s  total %d items across %d days", ch_name, len(all_items), day - 2)
    return site_id, all_items

# ---------------------------------------------------------------------------
# Build programme element
# ---------------------------------------------------------------------------

def build_programme_el(item, ch_id, with_details):
    # Start time — try datetimeInUtc first, then other possible keys
    raw_dt = (
        item.get("datetimeInUtc") or
        item.get("datetime_in_utc") or
        item.get("startDateTimeUtc") or
        item.get("startTime") or
        item.get("start_time") or
        item.get("airTime") or
        item.get("scheduleStartTime") or
        ""
    )
    start_dt = parse_start_time(raw_dt)
    if not start_dt:
        return None

    dur_raw = (
        item.get("duration") or
        item.get("durationInSeconds") or
        item.get("runTime") or
        "00:30:00"
    )
    # duration might be seconds integer
    if isinstance(dur_raw, (int, float)):
        dur_secs = int(dur_raw)
    else:
        dur_secs = parse_duration(str(dur_raw))

    stop_dt = start_dt + timedelta(seconds=dur_secs)

    # Title
    title = (
        item.get("programmeTitle") or
        item.get("title") or
        item.get("programmeName") or
        item.get("name") or
        "Unknown"
    )

    # Description
    desc = (
        item.get("longSynopsis") or
        item.get("shortSynopsis") or
        item.get("synopsis") or
        item.get("description") or
        ""
    )

    # Optional detail call
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
        genres = [GENRE_MAP[sf.lower()] for sf in sub_filters if isinstance(sf, str) and sf.lower() in GENRE_MAP]
    if not genres and item.get("genre"):
        genres = [item["genre"]]

    ep_num = item.get("episodeNumber") or details.get("episodeNumber")
    sn_num = item.get("seasonNumber")  or details.get("seasonNumber")
    if not ep_num:
        m = re.search(r"Ep(\d+)$", item.get("title") or "")
        if m: ep_num = int(m.group(1))
    if not sn_num:
        m = re.search(r" S(\d+)", title)
        if m: sn_num = int(m.group(1))

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
        for d in directors: SubElement(cr, "director").text = d
        for a in actors:    SubElement(cr, "actor").text = a

    return prog

# ---------------------------------------------------------------------------
# Build channel element
# ---------------------------------------------------------------------------

def build_channel_el(ch):
    site_id = str(ch.get("id", ""))
    name    = ch.get("title") or site_id
    number  = str(ch.get("channelNumber") or ch.get("number") or "")
    logo    = ch.get("logoUrl") or ch.get("imageUrl") or ""

    el = Element("channel", id=site_id)
    SubElement(el, "display-name", lang="en").text = name
    if number: SubElement(el, "display-name").text = number
    if logo:   SubElement(el, "icon", src=logo)
    SubElement(el, "url").text = f"https://content.astro.com.my/channels/{number}"
    return el

# ---------------------------------------------------------------------------
# Load + trim existing XML
# ---------------------------------------------------------------------------

def load_existing_xml(path, keep_days):
    ch_map = {}; prog_map = {}
    if not path.exists():
        log.info("No existing XML — starting fresh.")
        return ch_map, prog_map
    try:
        root = et_parse(str(path)).getroot()
    except Exception as e:
        log.warning("Cannot parse existing XML (%s) — starting fresh.", e)
        return ch_map, prog_map
    cutoff = datetime.now(tz=MYT) - timedelta(days=keep_days)
    kept = dropped = 0
    for el in root.findall("channel"):
        ch_map[el.get("id", "")] = el
    for el in root.findall("programme"):
        dt = parse_xmltv_dt(el.get("start", ""))
        if dt and dt < cutoff:
            dropped += 1; continue
        prog_map.setdefault(el.get("channel", ""), []).append(el)
        kept += 1
    log.info("Loaded XML — %d channels | %d kept | %d expired removed.", len(ch_map), kept, dropped)
    return ch_map, prog_map

# ---------------------------------------------------------------------------
# Merge + write
# ---------------------------------------------------------------------------

def merge_and_write(out_path, api_channels, new_events, old_ch_map, old_prog_map, with_details):
    root = Element("tv")
    root.set("generator-info-name", "astro-epg-grabber")
    root.set("source-info-name",    "Astro Malaysia")
    root.set("source-info-url",     "https://content.astro.com.my/channels")

    seen = set()
    for ch in api_channels:
        root.append(build_channel_el(ch))
        seen.add(str(ch.get("id", "")))
    for ch_id, el in old_ch_map.items():
        if ch_id not in seen: root.append(el)

    merged = {}
    for ch_id, progs in old_prog_map.items():
        for p in progs:
            merged[(ch_id, p.get("start", ""))] = p

    new_added = replaced = skipped = 0
    for ch in api_channels:
        ch_id = str(ch.get("id", ""))
        for item in new_events.get(ch_id, []):
            el = build_programme_el(item, ch_id, with_details)
            if el is None: skipped += 1; continue
            key = (ch_id, el.get("start", ""))
            if key in merged: replaced += 1
            else:             new_added += 1
            merged[key] = el

    for _, el in sorted(merged.items()):
        root.append(el)

    total = len(merged)
    log.info(
        "Merge — %d total | %d new | %d updated | %d archive | %d skipped (no time).",
        total, new_added, replaced, total - new_added - replaced, skipped,
    )
    indent(root, space="  ")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(root).write(str(out_path), encoding="utf-8", xml_declaration=True)
    log.info("Saved %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Astro EPG grabber")
    p.add_argument("-o", "--output",   default="astro_epg.xml")
    p.add_argument("-w", "--workers",  type=int, default=5)
    p.add_argument("-c", "--channels", nargs="*", type=int)
    p.add_argument("--config")
    p.add_argument("--keep-days",      type=int, default=KEEP_PAST_DAYS)
    p.add_argument("--no-details",     action="store_true")
    p.add_argument("--list-channels",  action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

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
        log.error("No channels — exiting.")
        sys.exit(1)

    out_path = Path(args.output)
    old_ch_map, old_prog_map = load_existing_xml(out_path, args.keep_days)

    today = datetime.now(tz=MYT).replace(hour=0, minute=0, second=0, microsecond=0)
    log.info(
        "Fetching from %s for %d channel(s) with %d workers%s ...",
        today.strftime(DATE_FMT_DAY), len(api_channels), args.workers,
        " [no-details]" if args.no_details else " [with details]",
    )

    new_events = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect_events, ch, today): ch for ch in api_channels}
        for fut in as_completed(futures):
            try:
                site_id, items = fut.result()
                new_events[site_id] = items
            except Exception as e:
                log.error("Failed %s — %s", futures[fut].get("title"), e)

    total = sum(len(v) for v in new_events.values())
    log.info("Fetch done — %d total items.", total)

    merge_and_write(out_path, api_channels, new_events, old_ch_map, old_prog_map,
                    with_details=not args.no_details)


if __name__ == "__main__":
    main()
