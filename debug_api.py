#!/usr/bin/env python3
"""Debug: print exact API response structure."""
import json
import requests

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

print("=== Channel List ===")
r = requests.get("https://contenthub-api.eco.astro.com.my/channel/all.json", headers=HEADERS, timeout=30)
print(f"Status: {r.status_code}")
data = r.json()
channels = data.get("response", [])
print(f"Total channels: {len(channels)}")

# Pick channel 397 (Astro Vaanavil) or first channel
target = next((c for c in channels if c.get("id") == 397), channels[0] if channels else None)
if not target:
    print("No channels found!")
    exit(1)

ch_id = target.get("id")
print(f"\nUsing channel: {target.get('title')} (id={ch_id})")
print(f"Channel keys: {list(target.keys())}")

print(f"\n=== Schedule for channel {ch_id} ===")
r2 = requests.get(f"https://contenthub-api.eco.astro.com.my/channel/{ch_id}.json", headers=HEADERS, timeout=30)
print(f"Status: {r2.status_code}")
sched = r2.json()

print(f"Top-level keys: {list(sched.keys()) if isinstance(sched, dict) else 'NOT A DICT'}")

resp = sched.get("response", {})
print(f"response keys: {list(resp.keys()) if isinstance(resp, dict) else type(resp)}")

schedule = resp.get("schedule", {})
print(f"schedule type: {type(schedule)}")
print(f"schedule date keys (first 3): {list(schedule.keys())[:3]}")

# Print first date's first 2 items in full
for date_key in list(schedule.keys())[:1]:
    items = schedule[date_key]
    print(f"\n--- {date_key} ({len(items)} items) ---")
    print(f"First item FULL JSON:")
    print(json.dumps(items[0] if items else {}, indent=2, ensure_ascii=False))
    if len(items) > 1:
        print(f"\nSecond item FULL JSON:")
        print(json.dumps(items[1], indent=2, ensure_ascii=False))

