#!/usr/bin/env python3
"""
CamAtlas traffic-camera catalog builder.

Pulls public Department of Transportation (DOT) traffic cameras and writes them
to catalog/traffic_catalog.json, grouped by region and country. These are open
government feeds: no API key is needed for the sources turned on by default.

Most traffic cameras are still images that refresh every 30-120 seconds; the app
reloads them automatically. A few also offer a live video URL.

Run on a schedule (GitHub Actions does this for free, see README).
Only Python's standard library is used.

    python build_traffic_catalog.py

Key-based state feeds (New York, Washington, etc.) are listed in KEYED_SOURCES
and are skipped unless you provide their key as an environment variable.
"""
import datetime as dt
import json
import os
import pathlib
import ssl
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CATALOG_FILE = HERE / "catalog" / "traffic_catalog.json"
_CTX = ssl.create_default_context()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_json(url: str, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "CamAtlas/1.0"})
    with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
        return json.load(r)


def _f(v):
    try:
        return round(float(v), 5)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Source adapters. Each returns a list of normalized camera dicts.
# ---------------------------------------------------------------------------

def caltrans(_cfg) -> list:
    """California (Caltrans CWWP2). 12 districts, open JSON, no key."""
    out = []
    for d in range(1, 13):
        url = f"https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json"
        try:
            data = fetch_json(url)
        except Exception as e:  # one bad district must not stop the rest
            print(f"  Caltrans D{d:02d}: {e}", file=sys.stderr)
            continue
        for row in data.get("data", []):
            c = (row or {}).get("cctv", {})
            loc = c.get("location", {})
            img = c.get("imageData", {})
            image = (img.get("currentImageURL") or "").strip()
            video = (img.get("streamingVideoURL") or "").strip()
            if not image and not video:
                continue
            if (loc.get("inService") or "true").lower() == "false":
                continue
            lat, lon = _f(loc.get("latitude")), _f(loc.get("longitude"))
            name = (loc.get("locationName") or img.get("imageDescription") or "Traffic camera").strip()
            route = (loc.get("route") or "").strip()
            out.append({
                "id": f"caltrans-{d}-{c.get('index')}",
                "title": name,
                "city": (loc.get("nearbyPlace") or loc.get("county") or "").strip(),
                "region": "California",
                "country": "United States",
                "cc": "US",
                "lat": lat, "lon": lon,
                "image": image if image.startswith("http") else "",
                "video": video if video.startswith("http") else "",
                "route": route,
                "source": "Caltrans",
            })
    return out


def georgia(_cfg) -> list:
    """Georgia 511 (open JSON). Best-effort; skipped if the shape changes."""
    url = "https://511ga.org/api/v2/get/cameras?format=json&key=public"
    try:
        data = fetch_json(url)
    except Exception as e:
        print(f"  Georgia 511: {e}", file=sys.stderr)
        return []
    rows = data if isinstance(data, list) else data.get("cameras", data.get("data", []))
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        image = (r.get("ImageUrl") or r.get("imageUrl") or r.get("image") or "").strip()
        video = (r.get("VideoUrl") or r.get("videoUrl") or "").strip()
        if not (image.startswith("http") or video.startswith("http")):
            continue
        out.append({
            "id": f"ga-{r.get('Id') or r.get('id') or image[-24:]}",
            "title": (r.get("Location") or r.get("name") or "Traffic camera").strip(),
            "city": (r.get("County") or "").strip(),
            "region": "Georgia", "country": "United States", "cc": "US",
            "lat": _f(r.get("Latitude") or r.get("latitude")),
            "lon": _f(r.get("Longitude") or r.get("longitude")),
            "image": image if image.startswith("http") else "",
            "video": video if video.startswith("http") else "",
            "route": (r.get("Roadway") or r.get("route") or "").strip(),
            "source": "Georgia 511",
        })
    return out


# Sources that run with no key.
OPEN_SOURCES = [
    ("Caltrans (California)", caltrans, {}),
    ("Georgia 511", georgia, {}),
]

# Sources that need a free key. Give the env var and the app will include them.
# Fill the adapter in when you have the key; left here as a map for you.
KEYED_SOURCES = {
    "WSDOT_KEY": "Washington State DOT  (https://wsdot.wa.gov/traffic/api/)",
    "NY511_KEY": "New York 511          (https://511ny.org/developers)",
    "FL511_KEY": "Florida 511",
    "TXDOT_KEY": "Texas DriveTexas",
}


def run() -> dict:
    cams = {}
    for name, fn, cfg in OPEN_SOURCES:
        try:
            found = fn(cfg)
            print(f"{name}: {len(found)} cameras")
            for c in found:
                if c.get("id"):
                    cams[c["id"]] = c
        except Exception as e:
            print(f"{name}: FAILED {e}", file=sys.stderr)

    missing = [v for k, v in KEYED_SOURCES.items() if not os.environ.get(k)]
    if missing:
        print("Add a key to include: " + "; ".join(missing), file=sys.stderr)

    streams = list(cams.values())
    streams.sort(key=lambda c: (c.get("region", ""), c.get("city", ""), c.get("title", "")))
    catalog = {
        "updated": now_iso(),
        "count": len(streams),
        "sources": sorted({c["source"] for c in streams}),
        "cameras": streams,
    }
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(streams)} traffic cameras.")
    return catalog


if __name__ == "__main__":
    run()
