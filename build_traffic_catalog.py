#!/usr/bin/env python3
"""
CamAtlas traffic / street camera catalog builder (multi-source).

Pulls public government camera feeds worldwide into catalog/traffic_catalog.json,
grouped by region and country. There is no single worldwide feed, so each source
is its own adapter. Open sources (no key) run by default; keyed sources run only
when you provide the key as an environment variable / GitHub secret.

Add a source by appending to SOURCES. Each run logs how many each source
returned, so you can verify a new one in the GitHub Actions log and fix its
field mapping if needed.

    python build_traffic_catalog.py

Only Python's standard library is used.
"""
import datetime as dt
import json
import os
import pathlib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CATALOG_FILE = HERE / "catalog" / "traffic_catalog.json"
_CTX = ssl.create_default_context()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "CamAtlas/1.0"})
    with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
        return json.load(r)


def _f(v):
    try:
        return round(float(v), 5)
    except (TypeError, ValueError):
        return None


def _http(v):
    v = (v or "").strip()
    return v if v.startswith("http") else ""


# ===========================================================================
# Adapters. Each takes a config dict and returns normalized camera dicts:
#   id, title, city, region, country, cc, lat, lon, image, video, route, source
# ===========================================================================

def caltrans(cfg) -> list:
    """California (Caltrans CWWP2). 12 districts, open, no key. HLS video."""
    out = []
    for d in range(1, 13):
        url = f"https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json"
        try:
            data = fetch(url)
        except Exception as e:
            print(f"  Caltrans D{d:02d}: {e}", file=sys.stderr)
            continue
        for row in data.get("data", []):
            c = (row or {}).get("cctv", {})
            loc, img = c.get("location", {}), c.get("imageData", {})
            image, video = _http(img.get("currentImageURL")), _http(img.get("streamingVideoURL"))
            if not (image or video):
                continue
            if (loc.get("inService") or "true").lower() == "false":
                continue
            out.append({
                "id": f"caltrans-{d}-{c.get('index')}",
                "title": (loc.get("locationName") or img.get("imageDescription") or "Traffic camera").strip(),
                "city": (loc.get("nearbyPlace") or loc.get("county") or "").strip(),
                "region": "California", "country": "United States", "cc": "US",
                "lat": _f(loc.get("latitude")), "lon": _f(loc.get("longitude")),
                "image": image, "video": video,
                "route": (loc.get("route") or "").strip(), "source": "Caltrans",
            })
    return out


def tfl_london(cfg) -> list:
    """Transport for London JamCams. Open, no key (optional app_key). Images + mp4."""
    url = "https://api.tfl.gov.uk/Place/Type/JamCam"
    app_key = os.environ.get("TFL_KEY", "")
    if app_key:
        url += "?app_key=" + urllib.parse.quote(app_key)
    data = fetch(url)
    out = []
    for p in data if isinstance(data, list) else []:
        props = {a.get("key"): a.get("value") for a in p.get("additionalProperties", [])}
        if (props.get("available") or "true").lower() == "false":
            continue
        image, video = _http(props.get("imageUrl")), _http(props.get("videoUrl"))
        if not (image or video):
            continue
        out.append({
            "id": f"tfl-{p.get('id')}",
            "title": (p.get("commonName") or "London traffic camera").strip(),
            "city": "London", "region": "London",
            "country": "United Kingdom", "cc": "GB",
            "lat": _f(p.get("lat")), "lon": _f(p.get("lon")),
            "image": image, "video": video, "route": "", "source": "TfL London",
        })
    return out


def finland(cfg) -> list:
    """Finland (Fintraffic Digitraffic weathercams). Open, no key. Images ~10 min."""
    import gzip as _gzip
    hdr = {"User-Agent": "CamAtlas/1.0", "Digitraffic-User": "CamAtlas",
           "Accept": "application/json", "Accept-Encoding": "gzip"}
    req = urllib.request.Request(
        "https://tie.digitraffic.fi/api/weathercam/v1/stations", headers=hdr)
    with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = _gzip.decompress(raw)
        data = json.loads(raw)
    out = []
    for ft in data.get("features", []):
        g = ft.get("geometry", {}) or {}
        coords = g.get("coordinates") or [None, None]
        lon, lat = _f(coords[0]), _f(coords[1])
        pr = ft.get("properties", {}) or {}
        name = (pr.get("name") or "Finland road camera").strip()
        for preset in pr.get("presets", []) or []:
            if preset.get("inCollection") is False:
                continue
            pid = preset.get("id") or preset.get("presetId")
            if not pid:
                continue
            direction = (preset.get("presentationName") or preset.get("direction") or "").strip()
            out.append({
                "id": f"fi-{pid}",
                "title": name + (f" — {direction}" if direction else ""),
                "city": "", "region": "Finland",
                "country": "Finland", "cc": "FI",
                "lat": lat, "lon": lon,
                "image": f"https://weathercam.digitraffic.fi/{pid}.jpg",
                "video": "", "route": "", "source": "Digitraffic Finland",
            })
    return out


def singapore(cfg) -> list:
    """Singapore (data.gov.sg / LTA). Open, no key. Images ~1 min."""
    data = fetch("https://api.data.gov.sg/v1/transport/traffic-images")
    items = data.get("items", [])
    cams = items[0].get("cameras", []) if items else []
    out = []
    for c in cams:
        loc = c.get("location", {}) or {}
        image = _http(c.get("image"))
        if not image:
            continue
        cid = c.get("camera_id")
        out.append({
            "id": f"sg-{cid}",
            "title": f"Singapore camera {cid}",
            "city": "Singapore", "region": "Singapore",
            "country": "Singapore", "cc": "SG",
            "lat": _f(loc.get("latitude")), "lon": _f(loc.get("longitude")),
            "image": image, "video": "", "route": "", "source": "LTA Singapore",
        })
    return out


def rest511(cfg) -> list:
    """Generic adapter for the common 511 'GetCameras' JSON shape used by many
    US / Canada systems (IBI/CARS vendor). Runs only if cfg['env'] key is set.

    cfg: {host, region, country, cc, source, env (optional — omit for open)}
    """
    key = os.environ.get(cfg["env"], "") if cfg.get("env") else ""
    if cfg.get("env") and not key:
        return []
    # Build candidate URLs: the two common IBI/CARS path styles.
    host = cfg["host"]
    kq = f"&key={urllib.parse.quote(key)}" if key else ""
    candidates = [
        f"https://{host}/api/v2/get/cameras?format=json{kq}",
        f"https://{host}/api/getcameras?format=json{kq}",
    ]
    rows = []
    for url in candidates:
        try:
            data = fetch(url)
        except Exception as e:
            print(f"  {cfg['source']} ({url.split('//')[1].split('/')[0]}): {e}", file=sys.stderr)
            continue
        rows = data if isinstance(data, list) else (
            data.get("Cameras") or data.get("cameras") or data.get("data") or [])
        if rows:
            break
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("Disabled", "")).lower() == "true":
            continue
        image = _http(r.get("Url") or r.get("ImageUrl") or r.get("imageUrl") or r.get("image"))
        video = _http(r.get("VideoUrl") or r.get("videoUrl"))
        views = r.get("Views")
        if isinstance(views, list) and views and isinstance(views[0], dict):
            v0 = views[0]
            image = image or _http(v0.get("Url") or v0.get("ImageUrl"))
            video = video or _http(v0.get("VideoUrl"))
        if not (image or video):
            continue
        out.append({
            "id": f"{cfg['cc']}-{cfg['source']}-{r.get('Id') or r.get('id') or len(out)}",
            "title": str(r.get("Name") or r.get("Location") or r.get("RoadwayName") or "Traffic camera").strip(),
            "city": "", "region": cfg["region"],
            "country": cfg["country"], "cc": cfg["cc"],
            "lat": _f(r.get("Latitude") or r.get("latitude")),
            "lon": _f(r.get("Longitude") or r.get("longitude")),
            "image": image, "video": video,
            "route": str(r.get("RoadwayName") or r.get("Roadway") or "").strip(),
            "source": cfg["source"],
        })
    return out


def wsdot(cfg) -> list:
    """Washington State DOT highway cameras. Free key (AccessCode). Images."""
    key = os.environ.get("WSDOT_KEY", "")
    if not key:
        return []
    url = ("https://wsdot.wa.gov/Traffic/api/HighwayCameras/HighwayCamerasREST.svc/"
           "GetCamerasAsJson?AccessCode=" + urllib.parse.quote(key))
    try:
        data = fetch(url)
    except Exception as e:
        print(f"  WSDOT: {e}", file=sys.stderr)
        return []
    out = []
    for c in data if isinstance(data, list) else []:
        if c.get("IsActive") is False:
            continue
        loc = c.get("CameraLocation", {}) or {}
        image = _http(c.get("ImageURL"))
        if not image:
            continue
        out.append({
            "id": f"wsdot-{c.get('CameraID')}",
            "title": (c.get("Title") or loc.get("Description") or "Washington camera").strip(),
            "city": "", "region": "Washington",
            "country": "United States", "cc": "US",
            "lat": _f(loc.get("Latitude")), "lon": _f(loc.get("Longitude")),
            "image": image, "video": "",
            "route": (loc.get("RoadName") or "").strip(), "source": "WSDOT",
        })
    return out


def _csv_rows(text):
    import csv, io
    return list(csv.DictReader(io.StringIO(text)))


def hong_kong(cfg) -> list:
    """Hong Kong Transport Department traffic snapshot images. Open, no key.
    Camera-location CSV gives key, region, lat/lon and the JPEG image URL."""
    url = ("https://static.data.gov.hk/td/traffic-snapshot-images/"
           "code/Traffic_Camera_Locations_En.csv")
    req = urllib.request.Request(url, headers={"User-Agent": "CamAtlas/1.0"})
    with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
        text = r.read().decode("utf-8-sig", "ignore")
    import csv as _csv
    rows = list(_csv.DictReader(text.splitlines()))
    out = []
    for r in rows:
        low = {}
        for k, v in r.items():
            if not k:
                continue
            low[k.lower().strip()] = v.strip() if isinstance(v, str) else ""
        image = _http(low.get("url") or low.get("image") or low.get("image_url"))
        if not image:
            continue
        out.append({
            "id": f"hk-{low.get('key') or low.get('camera_id') or len(out)}",
            "title": low.get("description") or low.get("location") or "Hong Kong camera",
            "city": low.get("district") or "", "region": low.get("region") or "Hong Kong",
            "country": "Hong Kong", "cc": "HK",
            "lat": _f(low.get("latitude")), "lon": _f(low.get("longitude")),
            "image": image, "video": "", "route": "", "source": "HK Transport Dept",
        })
    return out


def korea_its(cfg) -> list:
    """South Korea national ITS CCTV. Free key (serviceKey). HLS live video.
    Queries expressways (ex) and national highways (its) across the country."""
    key = os.environ.get("KR_ITS_KEY", "")
    if not key:
        return []
    out = []
    for kind in ("ex", "its"):
        q = urllib.parse.urlencode({
            "apiKey": key, "type": kind, "cctvType": "1",
            "minX": "124", "maxX": "132", "minY": "33", "maxY": "39",
            "getType": "json",
        })
        try:
            data = fetch(f"https://openapi.its.go.kr:9443/cctvInfo?{q}")
        except Exception as e:
            print(f"  Korea ITS ({kind}): {e}", file=sys.stderr)
            continue
        items = (((data or {}).get("response") or {}).get("data")) or []
        for c in items:
            video = _http(c.get("cctvurl"))
            if not video:
                continue
            out.append({
                "id": f"kr-{kind}-{c.get('cctvname') or len(out)}",
                "title": (c.get("cctvname") or "Korea road camera").strip(),
                "city": "", "region": "South Korea",
                "country": "South Korea", "cc": "KR",
                "lat": _f(c.get("coordy")), "lon": _f(c.get("coordx")),
                "image": "", "video": video, "route": "", "source": "Korea ITS",
            })
    return out


def openstreetmap(cfg) -> list:
    """Public webcams mapped on OpenStreetMap (contact:webcam tag). Open, no key,
    worldwide. Crowdsourced, so quality varies; URLs may be images, streams or
    viewer pages (pages open in the browser)."""
    query = '[out:json][timeout:120];(nwr["contact:webcam"];);out center tags;'
    url = "https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(query)
    data = fetch(url)
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {}) or {}
        link = _http(tags.get("contact:webcam"))
        if not link:
            continue
        center = el.get("center", {}) or {}
        lat = el.get("lat", center.get("lat"))
        lon = el.get("lon", center.get("lon"))
        base = link.lower().split("?")[0]
        is_img = base.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
        is_vid = base.endswith((".m3u8", ".mp4", ".ts"))
        country = tags.get("addr:country") or ""
        region = tags.get("addr:state") or tags.get("addr:city") or "OpenStreetMap"
        out.append({
            "id": f"osm-{el.get('type','n')[0]}{el.get('id')}",
            "title": (tags.get("name") or tags.get("description")
                      or tags.get("addr:city") or "Webcam").strip(),
            "city": tags.get("addr:city") or "",
            "region": region,
            "country": country or "Other (OpenStreetMap)",
            "cc": (tags.get("addr:country") or "").upper(),
            "lat": _f(lat), "lon": _f(lon),
            # image only if it clearly is one; otherwise treat as a stream/page.
            "image": link if is_img else "",
            "video": link if (is_vid or not is_img) else "",
            "route": "", "source": "OpenStreetMap",
        })
    return out


def nps_usa(cfg) -> list:
    """US National Park Service webcams. Free key (NPS_KEY). Scenic park cams."""
    key = os.environ.get("NPS_KEY", "")
    if not key:
        return []
    url = ("https://developer.nps.gov/api/v1/webcams?limit=2000&api_key="
           + urllib.parse.quote(key))
    try:
        data = fetch(url)
    except Exception as e:
        print(f"  NPS: {e}", file=sys.stderr)
        return []
    out = []
    for c in data.get("data", []):
        if (c.get("status") or "Active") != "Active":
            continue
        imgs = c.get("images") or []
        image = _http(imgs[0].get("url")) if imgs and isinstance(imgs[0], dict) else ""
        video = _http(c.get("streamUrl"))
        if not (image or video):
            continue
        lat = _f(c.get("latitude"))
        lon = _f(c.get("longitude"))
        rel = c.get("relatedParks") or []
        park = rel[0].get("fullName") if rel and isinstance(rel[0], dict) else ""
        out.append({
            "id": f"nps-{c.get('id') or c.get('title')}",
            "title": (c.get("title") or "Park webcam").strip(),
            "city": park or "", "region": "US National Parks",
            "country": "United States", "cc": "US",
            "lat": lat, "lon": lon,
            "image": image, "video": video, "route": "", "source": "US NPS",
        })
    return out


def arcgis(cfg) -> list:
    """Generic ESRI ArcGIS FeatureServer/MapServer layer query. Many DOTs use this.

    cfg: {url, region, country, cc, source, fields:{title,city,image,video,route}}
    The layer URL should end in the layer number, e.g. .../CCTV/MapServer/0
    """
    f = cfg["fields"]
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "where": "1=1", "outFields": "*", "f": "json",
            "returnGeometry": "true", "outSR": "4326",
            "resultOffset": offset, "resultRecordCount": 1000,
        })
        try:
            data = fetch(f"{cfg['url']}/query?{q}")
        except Exception as e:
            print(f"  {cfg['source']}: {e}", file=sys.stderr)
            break
        feats = data.get("features", [])
        if not feats:
            break
        for ft in feats:
            a = ft.get("attributes", {})
            g = ft.get("geometry", {}) or {}
            image = _http(a.get(f.get("image", "")))
            video = _http(a.get(f.get("video", "")))
            if not (image or video):
                continue
            out.append({
                "id": f"{cfg['cc']}-{cfg['source']}-{a.get(f.get('id','OBJECTID')) or len(out)}",
                "title": str(a.get(f.get("title", ""), "") or "Traffic camera").strip(),
                "city": str(a.get(f.get("city", ""), "") or "").strip(),
                "region": cfg["region"], "country": cfg["country"], "cc": cfg["cc"],
                "lat": _f(g.get("y")), "lon": _f(g.get("x")),
                "image": image, "video": video,
                "route": str(a.get(f.get("route", ""), "") or "").strip(),
                "source": cfg["source"],
            })
        if not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
    return out


# ===========================================================================
# SOURCES. Open sources run by default. A source with "env" set runs only if
# that environment variable (GitHub secret) is present.
# Add new sources here; verify each in the Actions log, then fix fields.
# ===========================================================================
SOURCES = [
    {"name": "California (Caltrans)", "fn": caltrans, "cfg": {}},
    {"name": "London (TfL)", "fn": tfl_london, "cfg": {}},
    {"name": "Finland (Digitraffic)", "fn": finland, "cfg": {}},
    {"name": "Singapore (data.gov.sg)", "fn": singapore, "cfg": {}},

    # 511 systems (US / Canada) — one shared format (rest511).
    # Open (no key):
    {"name": "Alberta (511)", "fn": rest511, "env": "AB511_KEY",
     "cfg": {"host": "511.alberta.ca", "region": "Alberta", "country": "Canada", "cc": "CA", "source": "511AB", "env": "AB511_KEY"}},
    # Keyed — each turns on automatically once its GitHub secret is set:
    {"name": "New York (511NY)", "fn": rest511, "env": "NY511_KEY",
     "cfg": {"host": "511ny.org", "region": "New York", "country": "United States", "cc": "US", "source": "511NY", "env": "NY511_KEY"}},
    {"name": "Georgia (511GA)", "fn": rest511, "env": "GA511_KEY",
     "cfg": {"host": "511ga.org", "region": "Georgia", "country": "United States", "cc": "US", "source": "511GA", "env": "GA511_KEY"}},
    {"name": "Louisiana (511LA)", "fn": rest511, "env": "LA511_KEY",
     "cfg": {"host": "511la.org", "region": "Louisiana", "country": "United States", "cc": "US", "source": "511LA", "env": "LA511_KEY"}},
    {"name": "Wisconsin (511WI)", "fn": rest511, "env": "WI511_KEY",
     "cfg": {"host": "511wi.gov", "region": "Wisconsin", "country": "United States", "cc": "US", "source": "511WI", "env": "WI511_KEY"}},
    {"name": "Idaho (511ID)", "fn": rest511, "env": "ID511_KEY",
     "cfg": {"host": "511.idaho.gov", "region": "Idaho", "country": "United States", "cc": "US", "source": "511ID", "env": "ID511_KEY"}},
    {"name": "Alaska (511AK)", "fn": rest511, "env": "AK511_KEY",
     "cfg": {"host": "511.alaska.gov", "region": "Alaska", "country": "United States", "cc": "US", "source": "511AK", "env": "AK511_KEY"}},
    {"name": "Nevada (511NV)", "fn": rest511, "env": "NV511_KEY",
     "cfg": {"host": "prod-nv.ibi511.com", "region": "Nevada", "country": "United States", "cc": "US", "source": "511NV", "env": "NV511_KEY"}},
    {"name": "Utah (UDOT)", "fn": rest511, "env": "UT511_KEY",
     "cfg": {"host": "prod-ut.ibi511.com", "region": "Utah", "country": "United States", "cc": "US", "source": "511UT", "env": "UT511_KEY"}},
    {"name": "Arizona (AZ511)", "fn": rest511, "env": "AZ511_KEY",
     "cfg": {"host": "az511.com", "region": "Arizona", "country": "United States", "cc": "US", "source": "AZ511", "env": "AZ511_KEY"}},
    {"name": "Newfoundland (511NL)", "fn": rest511, "env": "NL511_KEY",
     "cfg": {"host": "511nl.ca", "region": "Newfoundland and Labrador", "country": "Canada", "cc": "CA", "source": "511NL", "env": "NL511_KEY"}},
    {"name": "Yukon (511YT)", "fn": rest511, "env": "YT511_KEY",
     "cfg": {"host": "511yukon.ca", "region": "Yukon", "country": "Canada", "cc": "CA", "source": "511YT", "env": "YT511_KEY"}},
    {"name": "Ontario (511ON)", "fn": rest511, "env": "ON511_KEY",
     "cfg": {"host": "511on.ca", "region": "Ontario", "country": "Canada", "cc": "CA", "source": "511ON", "env": "ON511_KEY"}},
    {"name": "Manitoba (511MB)", "fn": rest511, "env": "MB511_KEY",
     "cfg": {"host": "manitoba511.ca", "region": "Manitoba", "country": "Canada", "cc": "CA", "source": "511MB", "env": "MB511_KEY"}},
    {"name": "New Brunswick (511NB)", "fn": rest511, "env": "NB511_KEY",
     "cfg": {"host": "511.gnb.ca", "region": "New Brunswick", "country": "Canada", "cc": "CA", "source": "511NB", "env": "NB511_KEY"}},

    # Washington State DOT (free key, own format):
    {"name": "Washington (WSDOT)", "fn": wsdot, "env": "WSDOT_KEY", "cfg": {}},

    # Asia:
    {"name": "Hong Kong (Transport Dept)", "fn": hong_kong, "cfg": {}},  # open
    {"name": "South Korea (ITS)", "fn": korea_its, "env": "KR_ITS_KEY", "cfg": {}},

    # Cross-border open / keyed sources:
    {"name": "OpenStreetMap webcams", "fn": openstreetmap, "cfg": {}},  # open, worldwide
    {"name": "US National Parks (NPS)", "fn": nps_usa, "env": "NPS_KEY", "cfg": {}},

    # Example ArcGIS source (left off until its URL/fields are verified):
    # {"name": "Example DOT", "fn": arcgis, "cfg": {
    #     "url": "https://example.dot.gov/arcgis/rest/services/CCTV/MapServer/0",
    #     "region": "Example", "country": "United States", "cc": "US",
    #     "source": "Example DOT",
    #     "fields": {"id": "OBJECTID", "title": "Name", "city": "City",
    #                "image": "ImageURL", "video": "VideoURL", "route": "Route"},
    # }},
]

# Keyed sources you can register for (free). Give me the key and I wire them in.
REGISTER_FOR = {
    "NY511_KEY": "New York      — https://511ny.org/developers",
    "GA511_KEY": "Georgia       — https://511ga.org/developers",
    "LA511_KEY": "Louisiana     — https://511la.org/developers",
    "WI511_KEY": "Wisconsin     — https://511wi.gov/developers/doc",
    "AZ511_KEY": "Arizona       — https://az511.com/developers/doc",
    "NL511_KEY": "Newfoundland  — https://511nl.ca/developers/doc",
    "YT511_KEY": "Yukon         — https://511yukon.ca/developers/doc",
    "AB511_KEY": "Alberta       — https://511.alberta.ca/developers/doc",
    "ID511_KEY": "Idaho         — https://511.idaho.gov/developers",
    "AK511_KEY": "Alaska        — https://511.alaska.gov/developers",
    "NV511_KEY": "Nevada        — https://www.nvroads.com/developers",
    "UT511_KEY": "Utah          — https://www.udottraffic.utah.gov/developers",
    "ON511_KEY": "Ontario       — https://511on.ca/developers",
    "MB511_KEY": "Manitoba      — https://www.manitoba511.ca/developers",
    "NB511_KEY": "New Brunswick — https://511.gnb.ca/developers",
    "WSDOT_KEY": "Washington    — https://wsdot.wa.gov/traffic/api/",
    "KR_ITS_KEY": "South Korea  — https://www.its.go.kr/opendata/",
    "NPS_KEY": "US Nat. Parks — https://www.nps.gov/subjects/developer/get-started.htm",
}


def run() -> dict:
    cams = {}
    for src in SOURCES:
        env = src.get("env")
        if env and not os.environ.get(env):
            print(f"{src['name']}: skipped (set {env} to enable)")
            continue
        try:
            found = src["fn"](src["cfg"])
            print(f"{src['name']}: {len(found)} cameras")
            for c in found:
                if c.get("id"):
                    cams[c["id"]] = c
        except Exception as e:
            print(f"{src['name']}: FAILED {e}", file=sys.stderr)

    todo = [v for k, v in REGISTER_FOR.items() if not os.environ.get(k)]
    if todo:
        print("Register (free) to add more:\n  " + "\n  ".join(todo), file=sys.stderr)

    streams = list(cams.values())
    streams.sort(key=lambda c: (c.get("country", ""), c.get("region", ""),
                                c.get("city", ""), c.get("title", "")))
    catalog = {
        "updated": now_iso(),
        "count": len(streams),
        "sources": sorted({c["source"] for c in streams}),
        "countries": sorted({c["country"] for c in streams}),
        "cameras": streams,
    }
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(streams)} cameras from {len(catalog['sources'])} sources, "
          f"{len(catalog['countries'])} countries.")
    return catalog


if __name__ == "__main__":
    run()
