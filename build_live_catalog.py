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
CAM_WORDS = [r"cam\b", r"\bcams\b", r"webcam", r"camera", r"kamera", r"camara",
             r"telecamera", r"\blive\b", r"live ?stream", r"ライブカメラ", r"en vivo",
             r"en direct", r"ao vivo", r"canli", r"라이브캠", r"\b24/7\b", r"panorama"]
# ...and not be one of these.
BLOCK_WORDS = [r"news", r"music", r"radio", r"lofi", r"lo-fi", r"gaming", r"minecraft",
               r"fortnite", r"\bgta\b", r"podcast", r"sermon", r"asmr", r"sleep",
               r"study", r"reaction", r"trading", r"crypto", r"bitcoin", r"forex",
               r"\bchat\b", r"\birl\b", r"\bwalk\b", r"walking", r"walking tour", r"driving",
               r"\bdrive\b", r"sexy", r"bikini", r"\bhot\b", r"girl", r"babe", r"nude",
               r"onlyfans", r"dating", r"scanner", r"#shorts", r"sounds", r"relax",
               r"meditation", r"prank", r"giveaway", r"concert", r"\bvs\.?\b",
               r"around the world", r"random cams", r"top live webcams", r"rolling cam",
               r"interview", r"press conference", r"election", r"debate", r"protest",
               r"clashes", r"riot", r"breaking", r"\bwar\b", r"attack", r"santa claus", r"santa tracker",
               r"north pole", r"nice list", r"countdown", r"re-live", r"\btv live\b"]
# Channels to skip (news/TV stations rather than webcam owners).
BLOCK_CHANNELS = [r"news", r"\btv\b", r"crux", r"times", r"post", r"cnn", r"bbc", r"sky "]
# Names that mean "this is a different place with the same name".
NOT_HERE = {
    "Venice": ["venice beach", "boardwalk", "los angeles", "california"],
    "Geneva": ["lake geneva, wi", "wisconsin", "illinois", "ohio"],
    "Nice": ["nice list"],
    "Monaco": ["hotel monaco"],
    "Chamonix": ["cripple creek", "colorado"],
}
US_STATES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado",
             "connecticut", "delaware", "florida", "idaho", "illinois", "indiana",
             "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
             "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
             "montana", "nebraska", "nevada", "new hampshire", "new jersey",
             "new mexico", "north carolina", "north dakota", "ohio", "oklahoma",
             "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
             "tennessee", "texas", "utah", "vermont", "virginia", "wisconsin", "wyoming"]
# Place names that are also common words or exist in several countries:
# never used to reject a stream as "other place".
AMBIGUOUS = {"nice", "split", "como", "porto", "turkey", "jordan", "georgia", "chile",
             "wellington", "perth", "halifax", "brighton", "santiago", "victoria",
             "manchester", "geneva", "venice", "monaco", "lima", "bergen", "cancun",
             "medina", "aruba", "malta", "fiji", "bali", "sicily", "cusco"}
_CONFLICTS = {}  # filled by build_queries: country -> names of other countries/cities


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


def has_any(text: str, patterns) -> bool:
    return any(re.search(p, text) for p in patterns)


def words_in(text: str, names) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(n), text) for n in names)


def place_ok(text: str, city: str, country: str) -> bool:
    """False when the text clearly points to a different place."""
    if city and words_in(text, NOT_HERE.get(city, [])):
        return False
    if country != "United States" and words_in(text, US_STATES):
        return False
    own = set(ALIASES.get(city, [norm(city)])) if city else set()
    own |= set(COUNTRY_ALIASES.get(country, [norm(country)]))
    for name in _CONFLICTS.get(country, []):
        # skip names that are part of our own names (e.g. "york" in "new york")
        if any(name in o or o in name for o in own):
            continue
        if re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", text):
            return False
    return True


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
        if e.code in (403, 429) and ("quota" in body.lower() or "ratelimit" in body.lower()):
            raise QuotaExceeded(body) from e
        raise


def build_queries(places):
    """One query per city, then one per country."""
    by_country = {}
    for p in places:
        names = ALIASES.get(p["city"], [norm(p["city"])])
        names = [n for n in names if len(n) >= 4 and n not in AMBIGUOUS]
        by_country.setdefault(p["country"], set()).update(names)
        by_country[p["country"]].update(
            n for n in COUNTRY_ALIASES.get(p["country"], [norm(p["country"])])
            if len(n) >= 4 and n not in AMBIGUOUS)
    _CONFLICTS.clear()
    for c in by_country:
        _CONFLICTS[c] = sorted({n for o, ns in by_country.items() if o != c for n in ns})
    qs = []
    for p in places:
        names = ALIASES.get(p["city"], [norm(p["city"])])
        qs.append({"q": f'{p["city"]} live cam', "city": p["city"], "country": p["country"],
                   "cc": p["cc"], "lat": p["lat"], "lon": p["lon"], "match": names,
                   "cmatch": COUNTRY_ALIASES.get(p["country"], [norm(p["country"])])})
    seen = set()
    for p in places:
        if p["cc"] in seen:
            continue
        seen.add(p["cc"])
        names = COUNTRY_ALIASES.get(p["country"], [norm(p["country"])])
        qs.append({"q": f'{p["country"]} live webcam', "city": "", "country": p["country"],
                   "cc": p["cc"], "lat": None, "lon": None, "match": names, "cmatch": []})
    return qs


def search(query, fetch):
    # Note: YouTube's eventType=live filter often returns nothing, so we search
    # normally and confirm which results are live right now in verify().
    data = fetch("search", {
        "part": "snippet", "type": "video",
        "maxResults": 50, "q": query["q"], "safeSearch": "moderate",
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
        if has_any(title, BLOCK_WORDS) or has_any(norm(sn.get("channelTitle", "")), BLOCK_CHANNELS):
            skipped.append(("blocked word", title))
            continue
        if not place_ok(text, query["city"], query["country"]):
            skipped.append(("other place", title))
            continue
        if words_in(text, query["match"]):
            city, lat, lon = query["city"], query["lat"], query["lon"]
        elif query.get("cmatch") and words_in(text, query["cmatch"]):
            city, lat, lon = "", None, None  # right country, other/unknown city
        else:
            skipped.append(("wrong place", title))
            continue
        found.append({
            "id": vid, "city": city, "country": query["country"], "cc": query["cc"],
            "lat": lat, "lon": lon, "firstSeen": now_iso(),
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
            t = norm(sn.get("title", ""))
            vtext = " ".join([t, norm(sn.get("description", ""))[:400], norm(sn.get("channelTitle", ""))])
            if has_any(t, BLOCK_WORDS) or has_any(norm(sn.get("channelTitle", "")), BLOCK_CHANNELS):
                continue
            entry = by_id[v["id"]]
            if not place_ok(vtext, entry.get("city", ""), entry.get("country", "")):
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
                old_s = streams.get(s["id"])
                if old_s is None or (not old_s.get("city") and s.get("city")):
                    streams[s["id"]] = s  # new, or now we know the city
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
