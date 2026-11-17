#!/usr/bin/env python3
"""
CamAtlas live-cam catalog builder.

Finds public 24/7 live webcam streams on YouTube for cities and countries
worldwide, checks they are really live and embeddable, and writes them to
catalog/live_catalog.json, grouped by country and city.

Run on a schedule (GitHub Actions does this for free, see README).
Only Python's standard library is used.

    YT_API_KEY=xxxx python build_live_catalog.py --budget 30

YouTube quota: 10,000 units/day. One search = 100 units, one status check of
up to 50 videos = 1 unit. --budget is the number of searches per run.
Searches rotate through places.json, so every place is covered every few days,
while all saved streams are re-checked on every run and removed when they end.
"""
import argparse
import datetime as dt
import html
import json
import os
import pathlib
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PLACES_FILE = HERE / "places.json"
CATALOG_FILE = HERE / "catalog" / "live_catalog.json"
API = "https://www.googleapis.com/youtube/v3"

# Extra spellings so a stream title matches the right place.
ALIASES = {
    "New York": ["new york", "nyc", "manhattan", "brooklyn"],
    "Times Square New York": ["times square"],
    "Abbey Road London": ["abbey road"],
    "Eiffel Tower": ["eiffel"],
    "Shibuya Crossing": ["shibuya"],
    "Giza Pyramids": ["giza", "pyramid"],
    "Amritsar Golden Temple": ["golden temple", "amritsar"],
    "Namibia waterhole": ["namibia", "namib"],
    "Kruger Park": ["kruger"],
    "Maasai Mara": ["maasai", "masai mara"],
    "Mount Everest": ["everest"],
    "Mount Fuji": ["fuji"],
    "Bondi Beach": ["bondi"],
    "Miami Beach": ["miami"],
    "Washington DC": ["washington"],
    "Lake Como": ["como"],
    "Ho Chi Minh City": ["ho chi minh", "saigon"],
    "Kyiv": ["kyiv", "kiev"],
    "Saint Petersburg": ["petersburg"],
    "Mecca": ["mecca", "makkah"],
    "Medina": ["medina", "madinah"],
    "Sicily": ["sicily", "sicilia"],
    "Zugspitze": ["zugspitze"],
    "Algarve": ["algarve"],
    "Rome": ["rome", "roma"],
    "Venice": ["venice", "venezia"],
    "Florence": ["florence", "firenze"],
    "Naples": ["naples", "napoli"],
    "Milan": ["milan", "milano"],
    "Munich": ["munich", "munchen"],
    "Cologne": ["cologne", "koln"],
    "Vienna": ["vienna", "wien"],
    "Prague": ["prague", "praha"],
    "Warsaw": ["warsaw", "warszawa"],
    "Krakow": ["krakow", "cracow"],
    "Lisbon": ["lisbon", "lisboa"],
    "Seville": ["seville", "sevilla"],
    "Copenhagen": ["copenhagen", "kobenhavn"],
    "Sao Paulo": ["sao paulo"],
    "Mexico City": ["mexico city", "cdmx", "ciudad de mexico"],
}
COUNTRY_ALIASES = {
    "United States": ["united states", "usa", "america"],
    "United Kingdom": ["united kingdom", "uk", "england", "scotland", "wales", "britain"],
    "United Arab Emirates": ["uae", "emirates", "dubai", "abu dhabi"],
    "Czech Republic": ["czech", "czechia"],
    "South Korea": ["korea"],
    "Netherlands": ["netherlands", "holland"],
}

# Title must look like a webcam...
CAM_WORDS = [r"\bcam\b", r"\bcams\b", r"webcam", r"livecam", r"camera", r"kamera",
             r"camara", r"telecamera", r"live view", r"ライブカメラ", r"en vivo",
             r"ao vivo", r"canli", r"라이브캠", r"\b24/7\b"]
# ...and not be one of these.
BLOCK_WORDS = [r"news", r"music", r"radio", r"lofi", r"lo-fi", r"gaming", r"minecraft",
               r"fortnite", r"\bgta\b", r"podcast", r"sermon", r"asmr", r"sleep",
               r"study", r"reaction", r"trading", r"crypto", r"bitcoin", r"forex",
               r"\bchat\b", r"\birl\b", r"walk", r"walking", r"\btour\b", r"driving",
               r"\bdrive\b", r"sexy", r"bikini", r"\bhot\b", r"girl", r"babe", r"nude",
               r"onlyfans", r"dating", r"scanner", r"#shorts", r"sounds", r"relax",
               r"meditation", r"prank", r"giveaway"]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


def has_any(text: str, patterns) -> bool:
    return any(re.search(p, text) for p in patterns)


def words_in(text: str, names) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", text) for n in names)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class QuotaExceeded(Exception):
    pass


def fetch_json(path: str, params: dict) -> dict:
    """GET a YouTube Data API endpoint. Replaced by a fake in tests."""
    params = dict(params, key=os.environ["YT_API_KEY"])
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        print(f"API error {e.code} on {path}: {body[:500]}", file=sys.stderr)
        if e.code == 403 and ("quota" in body.lower() or "rateLimit" in body):
            raise QuotaExceeded(body) from e
        raise


def build_queries(places):
    """One query per city, then one per country."""
    qs = []
    for p in places:
        names = ALIASES.get(p["city"], [norm(p["city"])])
        qs.append({"q": f'{p["city"]} live cam', "city": p["city"], "country": p["country"],
                   "cc": p["cc"], "lat": p["lat"], "lon": p["lon"], "match": names})
    seen = set()
    for p in places:
        if p["cc"] in seen:
            continue
        seen.add(p["cc"])
        names = COUNTRY_ALIASES.get(p["country"], [norm(p["country"])])
        qs.append({"q": f'{p["country"]} live webcam', "city": "", "country": p["country"],
                   "cc": p["cc"], "lat": None, "lon": None, "match": names})
    return qs


def search(query, fetch):
    data = fetch("search", {
        "part": "snippet", "type": "video", "eventType": "live",
        "maxResults": 25, "q": query["q"], "safeSearch": "strict",
    })
    found = []
    items = data.get("items", [])
    skipped = []
    for item in items:
        vid = item.get("id", {}).get("videoId")
        sn = item.get("snippet", {})
        title = norm(html.unescape(sn.get("title", "")))
        text = " ".join([title, norm(html.unescape(sn.get("description", ""))),
                         norm(sn.get("channelTitle", ""))])
        if not vid:
            continue
        if not has_any(title, CAM_WORDS):
            skipped.append(("not a cam", title))
            continue
        if has_any(title, BLOCK_WORDS):
            skipped.append(("blocked word", title))
            continue
        if not words_in(text, query["match"]):
            skipped.append(("wrong place", title))
            continue  # stream is not clearly about this place
        found.append({
            "id": vid, "city": query["city"], "country": query["country"], "cc": query["cc"],
            "lat": query["lat"], "lon": query["lon"], "firstSeen": now_iso(),
        })
    print(f'[{query["q"]}] results={len(items)} kept={len(found)}')
    for why, t in skipped[:4]:
        print(f"    skip ({why}): {t[:80]}")
    return found


def verify(streams, fetch):
    """Keep only public, embeddable streams that are live right now; refresh details."""
    by_id = {s["id"]: s for s in streams}
    ids = list(by_id)
    kept = []
    dropped = 0
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = fetch("videos", {
            "part": "snippet,liveStreamingDetails,status,recordingDetails",
            "id": ",".join(chunk),
        })
        for v in data.get("items", []):
            sn, st = v.get("snippet", {}), v.get("status", {})
            live = v.get("liveStreamingDetails", {})
            if sn.get("liveBroadcastContent") != "live" or "actualEndTime" in live:
                continue
            if not st.get("embeddable") or st.get("privacyStatus") != "public":
                continue
            if st.get("madeForKids"):
                continue
            if has_any(norm(sn.get("title", "")), BLOCK_WORDS):
                continue
            s = dict(by_id[v["id"]])
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
            loc = (v.get("recordingDetails") or {}).get("location") or {}
            if "latitude" in loc and "longitude" in loc:
                s["lat"], s["lon"] = round(loc["latitude"], 5), round(loc["longitude"], 5)
            s.update({
                "title": sn.get("title", "").strip(),
                "channel": sn.get("channelTitle", "").strip(),
                "thumb": thumb,
                "viewers": int(live.get("concurrentViewers", 0) or 0),
                "checked": now_iso(),
            })
            kept.append(s)
    dropped = len(ids) - len(kept)
    print(f"Checked {len(ids)} streams: {len(kept)} live and playable, {dropped} removed.")
    return kept


def run(budget: int, fetch=fetch_json) -> dict:
    places = json.loads(PLACES_FILE.read_text(encoding="utf-8"))["places"]
    queries = build_queries(places)
    old = {"cursor": 0, "streams": []}
    if CATALOG_FILE.exists():
        old = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))

    streams = {s["id"]: s for s in old.get("streams", [])}
    cursor = int(old.get("cursor", 0)) % max(len(queries), 1)
    searched = 0
    try:
        while searched < min(budget, len(queries)):
            q = queries[cursor]
            for s in search(q, fetch):
                streams.setdefault(s["id"], s)  # first placement wins
            cursor = (cursor + 1) % len(queries)
            searched += 1
    except QuotaExceeded:
        print("Quota reached; saving what we have.", file=sys.stderr)

    try:
        live = verify(list(streams.values()), fetch)
    except QuotaExceeded:
        print("Quota reached during checks; keeping previous list.", file=sys.stderr)
        live = old.get("streams", [])

    live.sort(key=lambda s: (-s.get("viewers", 0), s.get("country", ""), s.get("city", "")))
    catalog = {
        "updated": now_iso(),
        "source": "YouTube",
        "cursor": cursor,
        "count": len(live),
        "streams": live,
    }
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Searched {searched} places, {len(live)} live streams saved.")
    return catalog


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=30, help="searches per run (100 quota units each)")
    args = ap.parse_args()
    if not os.environ.get("YT_API_KEY"):
        sys.exit("Set YT_API_KEY first.")
    run(args.budget)
