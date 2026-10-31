#!/usr/bin/env python3
"""live_sync.py - server-side "new rental listings" scanner for the Nova Homes site.

Usage:
    live_sync.py <homes.json> [--write] [--log <path>]

Without --write: prints a summary to stderr and the new records as JSON to stdout.
With --write:    appends the new records to homes.json (existing records stay
                 byte-for-byte identical; new ones get checked=today and
                 new_since=<ISO UTC>) and writes the log/status file.
The log/status file (--log) is always rewritten when given, even if nothing new was
found, so the web page can show "last checked".

Exit 0 whether or not new homes were found; exit 2 only on a bug.

Standard library only (Python 3.11+), no browser, path independent.

Scope: for-rent houses, townhomes, duplexes in Tempe, Chandler, Gilbert, Mesa,
Ahwatukee (Phoenix 85044/85045/85048), Scottsdale, Paradise Valley and
Arcadia / Biltmore (Phoenix 85016/85018). Apartments / multi-unit complexes are
dropped. Data is never invented: a field a source does not give stays null/"".
Zumper is never used as listing_url (discovery only -> zumper_url).

Source probe results from the author's machine (residential IP, 2026-10-30):
  Zillow search pages (__NEXT_DATA__)          OK   - 41/page, newest-first, photos, lat/lng
  Trulia search pages (__NEXT_DATA__)          OK   - 40/page, newest-first, photos, manager
  Redfin stingray/api/v1/search/rentals (poly) OK   - bbox search; region lookup endpoints are 403
                                                      and city/home HTML pages return 202 (challenge)
  Invitation Homes /search/phoenix-arizona     OK   - JSON-LD graph with all metro listings (231)
  Progress Residential market search-results   OK   - JSON-LD ItemList, 200 rows/page (445 homes)
  AMH /query?criteria=Phoenix, AZ              OK   - __NEXT_DATA__ results with full records
  Tricon unit sitemap + home pages (JSON-LD)   OK   - no public search API (api/v1/homes 404)
  AppFolio <manager>.appfolio.com/listings     OK   - structured HTML cards
  Rentable city pages (AMP HTML)               OK   - tiles; sitemap.xml 500
  Craigslist phoenix.craigslist.org/search/apa OK   - static HTML results (RSS/format=rss is 403),
                                                      posting pages carry JSON-LD
  Zumper HTML (window.__PRELOADED_STATE__)     OK   - discovery only; api.zumper.com not reachable
  Realtor.com                                  BLOCKED (HTTP 429)        -> skipped
  HotPads                                      skipped (RSC flight stream, same Zillow inventory)
  FirstKey Homes                               skipped (client-rendered only, no public JSON)
On a GitHub Actions runner (datacenter IP) Zillow/Trulia/Redfin/Craigslist are the
likely ones to be blocked; each source fails independently and is reported in the log.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import http.client
import json
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 25
DEADLINE_S = 600          # whole run budget (10 min; every source is read in full so each home can be re-verified)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
T0 = time.monotonic()

FIELDS = ("name address city area type listing_kind rent_min rent_max beds_min beds_max baths "
          "sqft_min sqft_max year_built garage_spaces fenced_yard amenities dogs dog_weight_limit "
          "max_pets breed_restrictions husky husky_note pet_notes pet_deposit pet_fee pet_rent deposit "
          "deposit_min app_fee admin_fee specials available manager phone email website listing_url "
          "lat lng notes photos photo_count_listing source checked id").split()
STR_FIELDS = {"name", "address", "area", "dog_weight_limit", "breed_restrictions", "husky_note",
              "pet_notes", "pet_deposit", "pet_fee", "pet_rent", "deposit", "app_fee", "admin_fee",
              "specials", "available", "manager", "phone", "email", "website", "listing_url", "notes",
              "source"}
LIST_FIELDS = {"amenities", "photos"}
NEVER_WRITE = {"status", "status_at", "my_note", "note_at"}

CITY_ZIPS = {
    "Tempe": "85281 85282 85283 85284 85285 85287",
    "Chandler": "85224 85225 85226 85244 85246 85248 85249 85286",
    "Gilbert": "85233 85234 85295 85296 85297 85298 85299",
    "Mesa": "85201 85202 85203 85204 85205 85206 85207 85208 85209 85210 85211 85212 85213 85214 85215 85216",
    "Scottsdale": "85250 85251 85252 85254 85255 85256 85257 85258 85259 85260 85261 85262 85266 85267 85271",
    "Paradise Valley": "85253",
    "Ahwatukee": "85044 85045 85048",
    "Arcadia / Biltmore": "85016 85018",
}
ZIP_CITY = {z: c for c, zs in CITY_ZIPS.items() for z in zs.split()}
CITIES = set(CITY_ZIPS)
ID_PREFIX = {"Ahwatukee": "ahwatukee", "Arcadia / Biltmore": "arcadia", "Paradise Valley": "pv"}
TYPES = {"House", "Townhome", "Duplex"}
LOCALITY_CITY = {"tempe": "Tempe", "chandler": "Chandler", "gilbert": "Gilbert", "mesa": "Mesa",
                 "scottsdale": "Scottsdale", "paradise valley": "Paradise Valley"}

# ----------------------------------------------------------------------------- helpers
ABBR = {'street': 'st', 'drive': 'dr', 'avenue': 'ave', 'road': 'rd', 'lane': 'ln', 'north': 'n',
        'south': 's', 'east': 'e', 'west': 'w', 'place': 'pl', 'trail': 'trl', 'court': 'ct',
        'boulevard': 'blvd', 'circle': 'cir', 'unit': '#', 'apt': '#'}


def norm(a):
    """Address normalisation used for dedupe (same as expansion/merge_new.py)."""
    a = (a or '').lower().split(',')[0]
    a = re.sub(r'[^a-z0-9# ]', ' ', a)
    return ' '.join(ABBR.get(w, w) for w in a.split()).replace('# ', '#')


UNIT_RE = re.compile(r'(?:,\s*|\s+|\s*-\s*)(?:(?:unit|apt|apartment|ste|suite|lot|bldg)\.?\s*#?\s*|#\s*)([A-Za-z0-9-]+)|\s+-\s+(\d+[A-Za-z]?)\b', re.I)


def addr_parts(a):
    """-> (normalized street without unit, unit value or "", zip or "").

    "Unit X" / "Apt X" / "#X" / "Ste X" / "St - 1184" are all the same unit token; unit values
    na/none/n-a/null are dropped; the unit may sit after a comma ("..., Unit 5, City")."""
    a = a or ""
    m = re.search(r'\b(\d{5})(?:-\d{4})?\s*$', a.strip())
    zipc = m.group(1) if m else ""
    street = a.split(",")[0]
    rest = ",".join(a.split(",")[1:])
    mu = re.match(r'\s*(?:(?:unit|apt|apartment|ste|suite|lot|bldg)\.?\s*#?\s*|#\s*)([A-Za-z0-9-]+)\s*(?:,|$)', rest, re.I)
    unit = ""
    if mu:
        unit = mu.group(1)
    um = UNIT_RE.search(street)
    if um:
        unit = unit or (um.group(1) or um.group(2) or "")
        street = street[:um.start()]
    unit = unit.lower().strip("-")
    if unit in ("na", "n-a", "none", "null", "n"):
        unit = ""
    return norm(street), unit, zipc


def addr_keys(a):
    """(full key with unit, base key without unit, has_unit) for dedupe; zip included when known."""
    st, unit, z = addr_parts(a)
    return f"{st}#{unit}|{z}", f"{st}|{z}", bool(unit)


def addr_key(a):
    return addr_keys(a)[0]


def slug(s):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', (s or '').lower())).strip('-')


def time_left():
    return DEADLINE_S - (time.monotonic() - T0)


def get(url, headers=None, timeout=TIMEOUT, max_bytes=6_000_000):
    """Plain urllib GET. Raises on non-2xx / network error / deadline."""
    if time_left() < 5:
        raise RuntimeError("run deadline reached")
    h = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    h.setdefault("Accept-Encoding", "identity")
    req = urllib.request.Request(url, headers=h)
    last = None
    for attempt in range(2):          # one retry for truncated / dropped responses
        try:
            with urllib.request.urlopen(req, timeout=min(timeout, max(5, time_left()))) as r:
                if r.status // 100 != 2:
                    raise RuntimeError(f"HTTP {r.status}")
                chunks, n, truncated = [], 0, False
                while n < max_bytes:
                    try:
                        c = r.read(min(262144, max_bytes - n))
                    except http.client.IncompleteRead as e:
                        chunks.append(e.partial)
                        truncated = True
                        break
                    if not c:
                        break
                    chunks.append(c)
                    n += len(c)
                body = b"".join(chunks).decode("utf-8", "replace")
                if truncated:
                    raise http.client.IncompleteRead(body.encode("utf-8", "replace"))
                return body
        except (http.client.IncompleteRead, ConnectionResetError, TimeoutError, urllib.error.URLError) as e:
            if isinstance(e, urllib.error.HTTPError):
                raise
            last = e
            if time_left() < 10:
                break
            time.sleep(1.5)
    raise last


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.search(r'-?\d[\d,]*\.?\d*', str(v))
    if not m:
        return None
    s = m.group(0).replace(',', '')
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def intnum(v):
    n = num(v)
    return int(n) if isinstance(n, (int, float)) else None


def city_for(zipcode, locality=None):
    """City from ZIP (preferred) or locality. None => out of area."""
    z = (str(zipcode or "")[:5])
    loc = (locality or "").strip().lower()
    if loc in ("sun lakes", "queen creek", "san tan valley", "fountain hills", "guadalupe"):
        return None     # neighbouring towns that share a ZIP prefix with our cities
    if loc in LOCALITY_CITY and (not z or z in ZIP_CITY or loc != "scottsdale"):
        if z and z in ZIP_CITY and ZIP_CITY[z] != LOCALITY_CITY[loc] and ZIP_CITY[z] in ("Ahwatukee", "Arcadia / Biltmore"):
            return ZIP_CITY[z]
        if z == "85253" and loc == "scottsdale":
            return "Scottsdale"
        return LOCALITY_CITY[loc]
    if z in ZIP_CITY:
        return ZIP_CITY[z]
    return None


def is_street(addr):
    return bool(re.match(r'^\d+[A-Za-z]?\s+\S', (addr or '').strip()))


def date_only(s):
    if not s:
        return ""
    m = re.match(r'(\d{4}-\d{2}-\d{2})', str(s))
    return m.group(1) if m else str(s)


def type_from_text(text, default="House"):
    t = (text or "").lower()
    if re.search(r'\b(apartment|apt complex|condo|condominium|studio)\b', t) and not re.search(r'\b(town ?home|townhouse|duplex)\b', t):
        return None
    if re.search(r'\b(town ?home|townhouse|town house)\b', t):
        return "Townhome"
    if re.search(r'\bduplex\b', t):
        return "Duplex"
    if re.search(r'\b(house|home|casa|single[- ]family)\b', t):
        return "House"
    return default


def rec(**kw):
    """Build a record in the exact schema; unknown fields stay null / "" / []."""
    r = {}
    for k in FIELDS:
        if k in LIST_FIELDS:
            r[k] = []
        elif k in STR_FIELDS:
            r[k] = ""
        else:
            r[k] = None
    r["listing_kind"] = "Individual listing"
    for k, v in kw.items():
        if k in NEVER_WRITE:
            continue
        if v is None:
            continue
        if k in STR_FIELDS and not isinstance(v, str):
            v = str(v)
        r[k] = v
    r["photos"] = [p for p in dict.fromkeys(r.get("photos") or []) if isinstance(p, str) and p.startswith("https://")]
    if r.get("photo_count_listing") is None and r["photos"]:
        r["photo_count_listing"] = len(r["photos"])
    if r.get("rent_max") is None and r.get("rent_min") is not None:
        r["rent_max"] = r["rent_min"]
    if r.get("beds_max") is None and r.get("beds_min") is not None:
        r["beds_max"] = r["beds_min"]
    if r.get("sqft_max") is None and r.get("sqft_min") is not None:
        r["sqft_max"] = r["sqft_min"]
    return r


def ld_blocks(body):
    out = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', body, re.S | re.I):
        txt = m.group(1).strip()
        if not txt:
            continue
        try:
            out.append(json.loads(txt))
        except json.JSONDecodeError:
            try:
                out.append(json.loads(html.unescape(txt)))
            except json.JSONDecodeError:
                pass
    return out


def next_data(body):
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
    if not m:
        if "__NEXT_DATA__" in body:
            raise RuntimeError("truncated response (no closing </script> after __NEXT_DATA__)")
        return None
    return json.loads(m.group(1))


def find_key(x, key):
    if isinstance(x, dict):
        if key in x:
            return x[key]
        for v in x.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(x, list):
        for v in x:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def find_all(x, key, acc=None):
    acc = [] if acc is None else acc
    if isinstance(x, dict):
        for k, v in x.items():
            if k == key:
                acc.append(v)
            find_all(v, key, acc)
    elif isinstance(x, list):
        for v in x:
            find_all(v, key, acc)
    return acc


# ----------------------------------------------------------------------------- sources
# Each source: function(ctx) -> list[record]; raise to report an error.

ZILLOW_AREAS = ["tempe-az", "chandler-az", "gilbert-az", "mesa-az", "scottsdale-az", "paradise-valley-az",
                "phoenix-az-85044", "phoenix-az-85045", "phoenix-az-85048", "phoenix-az-85016", "phoenix-az-85018"]
ZILLOW_TYPES = {"SINGLE_FAMILY": "House", "TOWNHOUSE": "Townhome", "MULTI_FAMILY": "Duplex"}


def src_zillow(ctx):
    out = []
    # every page of every area (Zillow caps a search at 20 pages / 800 homes, so Scottsdale is split by rent)
    passes = [(a, None) for a in ZILLOW_AREAS if a != "scottsdale-az"] + [("scottsdale-az", {"max": 3999}), ("scottsdale-az", {"min": 4000})]
    incomplete = []
    for area, mp in passes:
        for page in range(1, 21):
            if time_left() < 60:
                incomplete.append(area); break
            fs = {"sort": {"value": "days"}, "fr": {"value": True}, "fsba": {"value": False},
                  "fsbo": {"value": False}, "nc": {"value": False}, "cmsn": {"value": False},
                  "auc": {"value": False}, "fore": {"value": False}, "apa": {"value": False},
                  "apco": {"value": False}, "con": {"value": False}, "land": {"value": False},
                  "manu": {"value": False}}
            if mp:
                fs["mp"] = {"value": mp}
            qs = {"pagination": ({"currentPage": page} if page > 1 else {}), "isMapVisible": False, "filterState": fs}
            url = f"https://www.zillow.com/{area}/rentals/?searchQueryState=" + urllib.parse.quote(json.dumps(qs, separators=(",", ":")))
            body = get(url)
            if "px-captcha" in body or "Press & Hold" in body:
                raise RuntimeError("blocked by PerimeterX captcha")
            d = next_data(body)
            if not d:
                raise RuntimeError("no __NEXT_DATA__ (blocked?)")
            results = find_key(d, "listResults") or []
            for r in results:
                hi = (r.get("hdpData") or {}).get("homeInfo") or {}
                typ = ZILLOW_TYPES.get(hi.get("homeType") or "")
                if not typ or not r.get("zpid"):
                    continue
                if (r.get("statusType") or hi.get("homeStatus")) not in (None, "FOR_RENT"):
                    continue
                street = r.get("addressStreet") or hi.get("streetAddress") or ""
                if not is_street(street):
                    continue
                zipc = r.get("addressZipcode") or hi.get("zipcode")
                city = city_for(zipc, r.get("addressCity") or hi.get("city"))
                if not city:
                    continue
                photos = []
                comp = r.get("carouselPhotosComposable") or {}
                if comp.get("baseUrl") and comp.get("photoData"):
                    photos = [comp["baseUrl"].replace("{photoKey}", p["photoKey"]) for p in comp["photoData"] if p.get("photoKey")]
                elif r.get("carouselPhotos"):
                    photos = [p.get("url") for p in r["carouselPhotos"] if p.get("url")]
                elif r.get("imgSrc"):
                    photos = [r["imgSrc"]]
                price_txt = r.get("price") or ""
                rent_min = intnum(r.get("unformattedPrice") or hi.get("price"))
                rent_max = rent_min
                if "+" in price_txt or " - " in price_txt:
                    nums = [intnum(x) for x in re.findall(r'\$[\d,]+', price_txt)]
                    nums = [n for n in nums if n]
                    if nums:
                        rent_min, rent_max = min(nums), (max(nums) if len(nums) > 1 else None)
                ll = r.get("latLong") or {}
                out.append(rec(
                    name=street, address=r.get("address") or f"{street}, {hi.get('city','')}, AZ {zipc}",
                    city=city, type=typ, rent_min=rent_min, rent_max=rent_max,
                    beds_min=intnum(r.get("beds") or hi.get("bedrooms")), baths=num(r.get("baths") or hi.get("bathrooms")),
                    sqft_min=intnum(r.get("area") or hi.get("livingArea")),
                    lat=ll.get("latitude") or hi.get("latitude"), lng=ll.get("longitude") or hi.get("longitude"),
                    listing_url=r.get("detailUrl") or "", photos=photos, manager=r.get("brokerName") or "",
                    available=date_only(r.get("availabilityDate")), source="Zillow",
                    specials=(hi.get("priceReduction") and f"Price reduced {hi['priceReduction']}") or ""))
            total_pages = int(find_key(d, "totalPages") or 1)
            if page >= total_pages:
                if total_pages >= 20 and (find_key(d, "totalResultCount") or 0) > 800:
                    incomplete.append(area)
                break
    ctx["complete"]["Zillow"] = not incomplete
    if incomplete:
        ctx["notes"]["Zillow"] = "not every page read for: " + ", ".join(sorted(set(incomplete)))
    return out


TRULIA_AREAS = ["Tempe,AZ", "Chandler,AZ", "Gilbert,AZ", "Mesa,AZ", "Scottsdale,AZ", "Paradise_Valley,AZ",
                "85044_zip", "85045_zip", "85048_zip", "85016_zip", "85018_zip"]
TRULIA_TYPES = {"single-family home": "House", "townhouse": "Townhome", "multi-family": "Duplex"}


def src_trulia(ctx):
    out = []
    # every page of every area: Trulia carries Zillow's inventory and still answers from the runner when Zillow 403s
    incomplete, seen_urls = [], set()
    for area in TRULIA_AREAS:
      for page in range(1, 41):
        if time_left() < 60:
            incomplete.append(area); break
        body = get(f"https://www.trulia.com/for_rent/{area}/SINGLE-FAMILY_HOME,TOWNHOUSE_type/date;d_sort/" + (f"{page}_p/" if page > 1 else ""))
        d = next_data(body)
        if not d:
            raise RuntimeError("no __NEXT_DATA__ (blocked?)")
        homes = find_key(d, "homes") or []
        fresh = [h for h in homes if isinstance(h, dict) and h.get("url") and h["url"] not in seen_urls]
        seen_urls.update(h["url"] for h in fresh)
        if not fresh:
            break
        total_pages = find_key(d, "totalPages") or find_key(d, "pageCount")
        for h in homes:
            if not isinstance(h, dict) or h.get("__typename") != "HOME_Property":
                continue
            loc = h.get("location") or {}
            street = re.sub(r'\s+', ' ', loc.get("streetAddress") or "").strip()
            if not is_street(street):
                continue
            trk = {t.get("key"): t.get("value") for t in (h.get("tracking") or []) if isinstance(t, dict)}
            ptype = (trk.get("propertyType") or "").lower()
            typ = TRULIA_TYPES.get(ptype)
            if not typ:
                continue
            cs = h.get("currentStatus") or {}
            if cs and cs.get("isActiveForRent") is False:
                continue
            city = city_for(loc.get("zipCode"), loc.get("city"))
            if not city:
                continue
            pr = h.get("price") or {}
            rent_min = intnum(pr.get("price") if "price" in pr else pr.get("min"))
            rent_max = intnum(pr.get("max")) if "max" in pr else None
            bd = h.get("bedrooms") or {}
            ba = h.get("bathrooms") or {}
            photos = []
            for p in ((h.get("media") or {}).get("photos") or []):
                u = (p.get("url") or {})
                photos.append(u.get("large") or u.get("medium") or u.get("small"))
            al = h.get("activeListing") or {}
            mgr = (((al.get("provider") or {}).get("listingAgent") or {}).get("name")
                   or ((al.get("provider") or {}).get("broker") or {}).get("name") or "")
            tags = {t.get("kind") for t in (h.get("tags") or []) if isinstance(t, dict)}
            co = loc.get("coordinates") or {}
            fs = (h.get("floorSpace") or {}).get("formattedDimension")
            out.append(rec(
                name=street, address=loc.get("formattedLocation") or f"{street}, {loc.get('city','')}, AZ {loc.get('zipCode','')}",
                city=city, type=typ, rent_min=rent_min, rent_max=rent_max,
                beds_min=intnum(bd.get("value") if "value" in bd else bd.get("min")),
                beds_max=intnum(bd.get("max")) if "max" in bd else None,
                baths=num(ba.get("value") if "value" in ba else ba.get("min")),
                sqft_min=intnum(fs), lat=co.get("latitude"), lng=co.get("longitude"),
                listing_url=("https://www.trulia.com" + h["url"]) if h.get("url") else "",
                photos=photos, manager=mgr, source="Trulia",
                pet_notes="Trulia tag: Pet friendly" if "PET_FRIENDLY" in tags else "",
                notes=(h.get("description") or "")[:600] if isinstance(h.get("description"), str) else ""))
        if (total_pages and page >= int(total_pages)) or len(homes) < 30:
            break
        if page == 40:
            incomplete.append(area)
    ctx["complete"]["Trulia"] = not incomplete
    if incomplete:
        ctx["notes"]["Trulia"] = "not every page read for: " + ", ".join(sorted(set(incomplete)))
    return out


REDFIN_BOXES = {  # (west, south, east, north)
    "Tempe": (-111.98, 33.32, -111.87, 33.47),
    "Chandler": (-111.98, 33.20, -111.78, 33.35),
    "Gilbert": (-111.83, 33.20, -111.68, 33.40),
    "Mesa": (-111.90, 33.33, -111.58, 33.50),
    "Scottsdale / PV": (-111.98, 33.45, -111.78, 33.80),
    "Ahwatukee": (-112.10, 33.27, -111.95, 33.35),
    "Arcadia / Biltmore": (-112.05, 33.49, -111.95, 33.54),
}
REDFIN_TYPES = {6: "House", 13: "Townhome", 4: "Duplex"}


def src_redfin(ctx):
    out = []
    incomplete = []
    # Redfin answers at most 350 homes per query and ignores page_number, so a box with more
    # matches is split into quadrants until each answers in full.
    boxes = [(name, box, 0) for name, box in REDFIN_BOXES.items()]
    seen_ids = set()
    while boxes:
        name, (w, s, e, n), depth = boxes.pop()
        if time_left() < 45:
            incomplete.append(name); continue
        poly = f"{w} {s},{e} {s},{e} {n},{w} {n},{w} {s}"
        url = ("https://www.redfin.com/stingray/api/v1/search/rentals?al=1&isRentals=true&num_homes=350"
               "&ord=days-on-redfin-asc&page_number=1&status=1&uipt=1,3,4&v=8&market=phoenix&poly=" + urllib.parse.quote(poly))
        body = get(url, headers={"Accept": "application/json"})
        body = body[body.find("{"):]
        d = json.loads(body)
        batch = d.get("homes") or []
        if (d.get("numMatchedHomes") or 0) > len(batch) and len(batch) >= 350:
            if depth < 4:
                mx, my = (w + e) / 2, (s + n) / 2
                boxes += [(name, (w, s, mx, my), depth + 1), (name, (mx, s, e, my), depth + 1),
                          (name, (w, my, mx, n), depth + 1), (name, (mx, my, e, n), depth + 1)]
                continue
            incomplete.append(name)
        for h in batch:
            pid = (h.get("homeData") or {}).get("propertyId")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            hd = h.get("homeData") or {}
            rx = h.get("rentalExtension") or {}
            typ = REDFIN_TYPES.get(hd.get("propertyType"))
            if not typ:
                continue
            if typ == "Duplex" and (rx.get("numAvailableUnits") or 1) > 2:
                continue
            ai = hd.get("addressInfo") or {}
            # Redfin tags single units inside apartment communities as propertyType 6; skip those
            pn = (rx.get("propertyName") or "")
            if (str(ai.get("unitNumber") or "").lower().startswith("apt") or rx.get("isCommercialPaid")
                    or (rx.get("numAvailableUnits") or 1) > 2
                    or re.search(r'\b(apartments?|apts|residences|community|homes at)\b', pn, re.I)
                    or (rx.get("bedRange") or {}).get("min") == 0):
                continue
            if (ai.get("state") or "AZ") != "AZ":
                continue
            street = ai.get("formattedStreetLine") or ""
            if not is_street(street):
                continue
            city = city_for(ai.get("zip"), ai.get("city"))
            if not city:
                continue
            cen = ((ai.get("centroid") or {}).get("centroid") or {})
            photos = []
            rid = rx.get("rentalId")
            for rg in ((hd.get("photosInfo") or {}).get("photoRanges") or []):
                for pos in range(int(rg.get("startPos", 0)), int(rg.get("endPos", -1)) + 1):
                    if rid and len(photos) < 20:
                        photos.append(f"https://ssl.cdn-redfin.com/photo/rent/{rid}/islphoto/genIsl.{pos}_{rg.get('version','1')}.jpg")
            rr = rx.get("rentPriceRange") or {}
            br = rx.get("bedRange") or {}
            bt = rx.get("bathRange") or {}
            sq = rx.get("sqftRange") or {}
            out.append(rec(
                name=street, address=f"{street}, {ai.get('city','')}, AZ {ai.get('zip','')}", city=city, type=typ,
                rent_min=intnum(rr.get("min")), rent_max=intnum(rr.get("max")),
                beds_min=intnum(br.get("min")), beds_max=intnum(br.get("max")),
                baths=num(bt.get("min")), sqft_min=intnum(sq.get("min")), sqft_max=intnum(sq.get("max")),
                lat=cen.get("latitude"), lng=cen.get("longitude"),
                listing_url=("https://www.redfin.com" + hd["url"]) if hd.get("url") else "",
                photos=photos, phone=rx.get("desktopPhone") or "", manager=rx.get("mlsName") or "",
                available=date_only(rx.get("dateAvailable")), notes=(rx.get("description") or "")[:600], source="Redfin"))
    ctx["complete"]["Redfin"] = not incomplete
    if incomplete:
        ctx["notes"]["Redfin"] = "not read in full: " + ", ".join(sorted(set(incomplete)))
    return out


def src_invitation(ctx):
    body = get("https://www.invitationhomes.com/search/phoenix-arizona")
    out = []
    for d in ld_blocks(body):
        for node in (d.get("@graph") or []) if isinstance(d, dict) else []:
            if node.get("@type") != "RealEstateListing":
                continue
            me = node.get("mainEntity") or {}
            ad = me.get("address") or {}
            street = ad.get("streetAddress") or ""
            if not is_street(street):
                continue
            city = city_for(ad.get("postalCode"), ad.get("addressLocality"))
            if not city:
                continue
            geo = me.get("geo") or {}
            of = node.get("offers") or {}
            out.append(rec(
                name=street, address=f"{street}, {ad.get('addressLocality','')}, AZ {ad.get('postalCode','')}",
                city=city, type="House", rent_min=intnum(of.get("price")),
                beds_min=intnum(me.get("numberOfBedrooms")), baths=num(me.get("numberOfBathroomsTotal")),
                sqft_min=intnum((me.get("floorSize") or {}).get("value")),
                lat=geo.get("latitude"), lng=geo.get("longitude"), listing_url=node.get("url") or "",
                photos=[me["image"]] if isinstance(me.get("image"), str) else list(me.get("image") or []),
                available=date_only(of.get("availabilityStarts")), manager="Invitation Homes",
                website="https://www.invitationhomes.com", source="Invitation Homes"))
    if not out and "RealEstateListing" not in body:
        raise RuntimeError("no JSON-LD listings in page")
    return out


def src_progress(ctx):
    out = []
    for page in range(1, 4):
        body = get(f"https://rentprogress.com/houses-for-rent/market-phoenix-az/page-{page}/rows-200/search-results/")
        items = []
        for d in ld_blocks(body):
            for node in (d.get("@graph") or []) if isinstance(d, dict) else []:
                me = node.get("mainEntity") or {}
                if isinstance(me, dict) and me.get("@type") == "ItemList":
                    items += [li.get("item") for li in me.get("itemListElement") or [] if isinstance(li, dict)]
        for it in items:
            if not isinstance(it, dict):
                continue
            url = it.get("url") or it.get("@id") or ""
            m = re.search(r'/property-details/[^/]+/([^/]+)/az/(\d{5})/', url)
            ab = it.get("about") or {}
            ad = ab.get("address") or {}
            street = ad.get("streetAddress") or ""
            zipc = ad.get("postalCode") or (m.group(2) if m else None)
            if not is_street(street):
                continue
            city = city_for(zipc, ad.get("addressLocality"))
            if not city:
                continue
            geo = ab.get("geo") or {}
            cat = (ab.get("accommodationCategory") or "House")
            typ = type_from_text(cat, "House") or "House"
            photos = list(ab.get("photo") or []) or ([it["image"]] if isinstance(it.get("image"), str) else [])
            out.append(rec(
                name=street, address=f"{street}, {ad.get('addressLocality','')}, AZ {zipc or ''}".strip(),
                city=city, type=typ, rent_min=intnum((it.get("offers") or {}).get("price")),
                beds_min=intnum(ab.get("numberOfBedrooms")), baths=num(ab.get("numberOfBathroomsTotal")),
                sqft_min=intnum((ab.get("floorSize") or {}).get("value")),
                lat=geo.get("latitude"), lng=geo.get("longitude"), listing_url=url, photos=photos,
                notes=(it.get("description") or "")[:600], manager="Progress Residential",
                website="https://rentprogress.com", source="Progress Residential"))
        if len(items) < 200:
            break
    return out


AMH_FEATURES = {"fenced yard": "Fenced yard", "pool": "Pool", "washer": "In-unit W/D", "dryer": "In-unit W/D",
                "garage": "Garage", "gated": "Gated", "smart home": "Smart home", "patio": "Balcony/patio"}


def src_amh(ctx):
    body = get("https://www.amh.com/query?criteria=" + urllib.parse.quote("Phoenix, AZ"))
    d = next_data(body)
    if not d:
        raise RuntimeError("no __NEXT_DATA__")
    pp = (d.get("props") or {}).get("pageProps") or {}
    out = []
    for p in pp.get("results") or []:
        street = p.get("addressLine1") or ""
        if not is_street(street) or (p.get("state") or "AZ") != "AZ":
            continue
        city = city_for(p.get("zipCode"), p.get("city"))
        if not city:
            continue
        amen, garage = [], None
        for f in p.get("features") or []:
            n = (f.get("name") or "").lower()
            for k, v in AMH_FEATURES.items():
                if k in n and v not in amen:
                    amen.append(v)
            mg = re.match(r'(\d) car garage', n)
            if mg:
                garage = int(mg.group(1))
        photos = [i.get("url_1024w") or i.get("url_original") for i in (p.get("images") or []) if isinstance(i, dict)]
        no = (p.get("propertyNo") or "").lower()
        # matches existing homes.json URLs, e.g. /p/az/chandler/2541-e-la-costa-drive-chandler-az-85249--az00005
        addr_slug = slug(f"{street} {p.get('city') or ''} az {p.get('zipCode') or ''}")
        url = f"https://www.amh.com/p/az/{slug(p.get('city'))}/{addr_slug}--{no}" if no else ""
        out.append(rec(
            name=street, address=f"{street}, {p.get('city','')}, AZ {p.get('zipCode','')}", city=city, type="House",
            rent_min=intnum(p.get("rent")), beds_min=intnum(p.get("bedrooms")), baths=num(p.get("bathrooms")),
            sqft_min=intnum(p.get("unitSqFt")), year_built=intnum(p.get("yearBuilt")), garage_spaces=garage,
            fenced_yard=True if "Fenced yard" in amen else None, amenities=amen,
            lat=p.get("latitude"), lng=p.get("longitude"), listing_url=url, photos=photos,
            available=date_only(p.get("availableDate") or p.get("readyDate")), phone=p.get("contactPhone") or "",
            notes=(p.get("propertyDesc") or "")[:600], manager="AMH", website="https://www.amh.com", source="AMH"))
    return out


TRICON_CITY_SLUGS = ("-tempe-az/", "-chandler-az/", "-gilbert-az/", "-mesa-az/", "-scottsdale-az/",
                     "-paradise-valley-az/", "-phoenix-az/")


def src_tricon(ctx):
    idx = get("https://triconhomes.com/sitemap.xml")
    maps = [u for u in re.findall(r'<loc>([^<]+)</loc>', idx) if "unit-sitemap" in u] or ["https://triconhomes.com/unit-sitemap1.xml"]
    urls = []
    for m in maps[:3]:
        urls += [u for u in re.findall(r'<loc>([^<]+)</loc>', get(m)) if u.endswith(TRICON_CITY_SLUGS)]
    new = [u for u in urls if u not in ctx["seen_url"] and u.rstrip("/") not in ctx["seen_url"]]
    out = []
    for u in new[:12]:
        body = get(u)
        price = intnum(next(iter(re.findall(r'"price"\s*:\s*"?(\d{3,5})', body)), None))
        for d in ld_blocks(body):
            nodes = (d.get("@graph") or [d]) if isinstance(d, dict) else []
            for node in nodes:
                if node.get("@type") not in ("SingleFamilyResidence", "House", "Residence", "Accommodation"):
                    continue
                ad = node.get("address") or {}
                street = ad.get("streetAddress") or ""
                if not is_street(street):
                    continue
                city = city_for(ad.get("postalCode"), ad.get("addressLocality"))
                if not city:
                    continue
                geo = node.get("geo") or {}
                imgs = node.get("image") or []
                photos = [i.get("url") if isinstance(i, dict) else i for i in (imgs if isinstance(imgs, list) else [imgs])]
                out.append(rec(
                    name=street, address=f"{street}, {ad.get('addressLocality','')}, AZ {ad.get('postalCode','')}",
                    city=city, type="House", rent_min=price, beds_min=intnum(node.get("numberOfBedrooms")),
                    baths=num(node.get("numberOfBathroomsTotal")), sqft_min=intnum((node.get("floorSize") or {}).get("value")),
                    year_built=intnum(node.get("yearBuilt")), lat=num(geo.get("latitude")), lng=num(geo.get("longitude")),
                    listing_url=u, photos=photos, manager="Tricon Residential", website="https://triconhomes.com",
                    source="Tricon Residential"))
    ctx["notes"]["Tricon Residential"] = f"{len(urls)} in-area homes in sitemap, {len(new)} not yet in homes.json"
    return out


def src_appfolio(ctx):
    subs = sorted(ctx["appfolio_subs"])[:30]
    out = []

    def one(sub):
        body = get(f"https://{sub}.appfolio.com/listings?filters%5Border_by%5D=date_posted", timeout=20)
        res = []
        for blk in re.split(r'<div class="listing-item result js-listing-item"', body)[1:]:
            blk = blk.split('<div class="listing-item result js-listing-item"')[0]
            href = re.search(r'href="(/listings/detail/[0-9a-f-]+)"', blk)
            addr = re.search(r'js-listing-address">\s*([^<]+?)\s*<', blk)
            title = re.search(r'js-listing-title">\s*<a[^>]*>\s*([^<]+?)\s*</a>', blk)
            rent = re.search(r'js-listing-blurb-rent">\s*([^<]+?)\s*<', blk)
            bb = re.search(r'js-listing-blurb-bed-bath">\s*([^<]+?)\s*<', blk)
            sq = re.search(r'Square Feet</dt>\s*<dd[^>]*>\s*([^<]+?)\s*<', blk)
            av = re.search(r'js-listing-available">\s*([^<]+?)\s*<', blk)
            img = re.search(r'data-original="(https://[^"]+)"', blk)
            desc = re.search(r'js-listing-description">(.*?)</p>', blk, re.S)
            if not (href and addr):
                continue
            full = re.sub(r'\s+', ' ', html.unescape(addr.group(1))).strip()
            # AppFolio often prefixes a unit label: "1446 - 9707 E Mountain View Rd #1446, ..." -> keep the street part
            if " - " in full.split(",")[0]:
                parts = [x.strip() for x in full.split(",")[0].split(" - ") if is_street(x.strip())]
                if parts:
                    full = parts[-1] + "," + ",".join(full.split(",")[1:])
            m = re.match(r'(.+?),\s*([A-Za-z .]+?),\s*AZ\s*(\d{5})', full)
            if not m or not is_street(m.group(1)) or re.search(r'\b(suite|ste)\b', m.group(1), re.I):
                continue
            city = city_for(m.group(3), m.group(2))
            if not city:
                continue
            ttl = html.unescape(title.group(1)) if title else ""
            dtext = re.sub(r'<[^>]+>', ' ', html.unescape(desc.group(1))) if desc else ""
            has_unit = bool(re.search(r'(?i)\b(unit|apt|#)\s*\w', m.group(1)))
            typ = type_from_text(ttl + " " + dtext[:300], None if has_unit else "House")
            if typ is None:
                continue
            beds = baths = None
            if bb:
                mb = re.search(r'([\d.]+)\s*bd\s*/\s*([\d.]+)\s*ba', bb.group(1))
                if mb:
                    beds, baths = intnum(mb.group(1)), num(mb.group(2))
            avail = html.unescape(av.group(1)).strip() if av else ""
            res.append(rec(
                name=m.group(1).strip(), address=full, city=city, type=typ, rent_min=intnum(rent.group(1) if rent else None),
                beds_min=beds, baths=baths, sqft_min=intnum(sq.group(1) if sq else None),
                listing_url=f"https://{sub}.appfolio.com{href.group(1)}", photos=[img.group(1)] if img else [],
                available=("Now" if avail.upper() == "NOW" else avail), manager=ctx["appfolio_mgr"].get(sub, ""),
                notes=re.sub(r'\s+', ' ', dtext).strip()[:600], website=f"https://{sub}.appfolio.com/listings",
                source="AppFolio"))
        return res

    errs = []
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        for sub, fut in [(s, ex.submit(one, s)) for s in subs]:
            try:
                out += fut.result()
            except Exception as e:  # one manager failing must not kill the source
                errs.append(f"{sub}: {str(e)[:60]}")
    ctx["notes"]["AppFolio"] = f"{len(subs)} manager sites; {len(errs)} failed" + (": " + "; ".join(errs[:4]) if errs else "")
    if errs and len(errs) == len(subs):
        raise RuntimeError("all AppFolio sites failed: " + errs[0])
    return out


RENTABLE_CITIES = ["tempe", "chandler", "gilbert", "mesa", "scottsdale", "paradise-valley"]


def src_rentable(ctx):
    out = []
    for c in RENTABLE_CITIES:
        try:
            body = get(f"https://www.rentable.co/{c}-az/houses-for-rent")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        for blk in re.split(r'<div class="property-tile-info-container">', body)[1:]:
            tail = blk[:6000]
            a = re.search(r'<a href="(/[a-z0-9-]+-az/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>', tail)
            rent = re.search(r'rent-range-item">\s*([^<]+?)\s*<', tail)
            cityt = re.search(r'property-address">\s*([^<]+?)\s*<', tail)
            beds = re.search(r'(\d+)\s*BR', tail)
            avail = re.search(r'fa-calendar-alt.*?</span>\s*([A-Z][a-z]+\.? \d{1,2}, \d{4}|Available Now|Now)', tail, re.S)
            if not (a and cityt and beds):
                continue
            if rent and re.search(r'\$[\d,]+\s*-\s*\$[\d,]+', rent.group(1)):
                continue          # rent range = several units = not a single house
            if int(beds.group(1)) < 2:
                continue          # 0-1 bed "houses" on Rentable are apartment units
            name = html.unescape(a.group(2))
            if not is_street(name):
                continue
            m = re.match(r'([A-Za-z .]+?),\s*AZ', html.unescape(cityt.group(1)))
            city = city_for(None, m.group(1) if m else None)
            if not city:
                continue
            typ = type_from_text(name, "House") or "House"
            out.append(rec(
                name=name, address=f"{name}, {m.group(1).strip()}, AZ", city=city, type=typ,
                rent_min=intnum(rent.group(1) if rent else None), beds_min=intnum(beds.group(1) if beds else None),
                listing_url="https://www.rentable.co" + a.group(1), available=avail.group(1) if avail else "",
                source="Rentable"))
        # one photo per tile lives before the info container
        imgs = re.findall(r'<amp-img class="property-img"[^>]*src="(https://images\.rentable\.co/[^"]+)"[^>]*alt="([^"]+)"', body)
        by_alt = {}
        for u, alt in imgs:
            by_alt.setdefault(html.unescape(alt).split(" Apartment for rent")[0].split(" House for rent")[0].strip(), u)
        for r in out:
            if not r["photos"] and r["name"] in by_alt:
                r["photos"] = [by_alt[r["name"]]]
                r["photo_count_listing"] = None
    return out


CL_AREAS = [("85281", "Tempe"), ("85225", "Chandler"), ("85234", "Gilbert"), ("85204", "Mesa"), ("85251", "Scottsdale"),
            ("85253", "Paradise Valley"), ("85044", "Ahwatukee"), ("85018", "Arcadia / Biltmore")]


def src_craigslist(ctx):
    out, cand = [], []
    for postal, _city in CL_AREAS:
        body = get(f"https://phoenix.craigslist.org/search/apa?housing_type=4&housing_type=6&housing_type=9&postal={postal}&search_distance=4")
        for m in re.finditer(r'<li class="cl-static-search-result" title="([^"]*)">\s*<a href="([^"]+)">(.*?)</li>', body, re.S):
            title, href, inner = html.unescape(m.group(1)), m.group(2), m.group(3)
            price = re.search(r'class="price">\s*([^<]+?)\s*<', inner)
            if href in ctx["seen_url"] or any(href == c[1] for c in cand):
                continue
            cand.append((title, href, intnum(price.group(1) if price else None)))
    fetched = 0
    for title, href, price in cand:
        if fetched >= 15 or time_left() < 40:
            break
        fetched += 1
        try:
            body = get(href)
        except Exception:
            continue
        for d in ld_blocks(body):
            if not isinstance(d, dict) or "address" not in d:
                continue
            ad = d.get("address") or {}
            street = ad.get("streetAddress") or ""
            if not is_street(street):
                continue
            city = city_for(ad.get("postalCode"), ad.get("addressLocality"))
            if not city:
                continue
            typ = {"House": "House", "SingleFamilyResidence": "House", "Townhouse": "Townhome"}.get(d.get("@type")) or type_from_text(title, "House")
            if typ is None:
                continue
            photos = list(dict.fromkeys(re.findall(r'(https://images\.craigslist\.org/[A-Za-z0-9_]+_600x450\.jpg)', body)))
            attrs = re.findall(r'<span class="attr[^"]*">\s*(?:<span class="labl">[^<]*</span>)?\s*<span class="valu">([^<]+)</span>', body)
            dogs = "Allowed" if re.search(r'dogs are OK|wooof', body) else None
            out.append(rec(
                name=street, address=f"{street}, {ad.get('addressLocality','')}, AZ {ad.get('postalCode','')}",
                city=city, type=typ, rent_min=price, beds_min=intnum(d.get("numberOfBedrooms")),
                baths=num(d.get("numberOfBathroomsTotal")), lat=num(d.get("latitude")), lng=num(d.get("longitude")),
                listing_url=href, photos=photos, dogs=dogs,
                pet_notes="Craigslist: pets allowed" if d.get("petsAllowed") is True else "",
                notes=("Craigslist: " + title + (" | " + ", ".join(a.strip() for a in attrs[:8]) if attrs else ""))[:600],
                source="Craigslist"))
            break
    ctx["notes"]["Craigslist"] = f"{len(cand)} new postings seen, {fetched} detail pages fetched"
    return out


ZUMPER_CITIES = ["tempe", "chandler", "gilbert", "mesa", "scottsdale", "paradise-valley"]


def src_zumper(ctx):
    out = []
    for c in ZUMPER_CITIES:
        body = get(f"https://www.zumper.com/houses-for-rent/{c}-az")
        i = body.find("window.__PRELOADED_STATE__")
        if i < 0:
            raise RuntimeError("no __PRELOADED_STATE__")
        j = body.find("</script>", i)
        txt = body[body.find("=", i) + 1:j].strip()
        state = json.JSONDecoder().raw_decode(txt)[0]
        items = []
        for lst in find_all(state, "listables"):       # several "listables" keys exist; take the list of dicts
            if isinstance(lst, list) and lst and isinstance(lst[0], dict) and len(lst) > len(items):
                items = lst
        for it in items:
            if not isinstance(it, dict):
                continue
            street = it.get("address") or ""
            if not is_street(street) or (it.get("state") or "AZ") != "AZ":
                continue
            city = city_for(it.get("zipcode"), it.get("city"))
            if not city:
                continue
            tags = " ".join(it.get("amenity_tags") or [])
            typ = type_from_text(it.get("title") or "", "House") or "House"
            pets = it.get("pets") or []
            amen = []
            for k, v in AMH_FEATURES.items():
                if k in tags.lower() and v not in amen:
                    amen.append(v)
            out.append(rec(
                name=street, address=f"{street}, {it.get('city','')}, AZ {it.get('zipcode','')}", city=city, type=typ,
                rent_min=intnum(it.get("min_price")), rent_max=intnum(it.get("max_price")),
                beds_min=intnum(it.get("min_bedrooms")), beds_max=intnum(it.get("max_bedrooms")),
                baths=num(it.get("min_bathrooms")), sqft_min=intnum(it.get("min_square_feet")), sqft_max=intnum(it.get("max_square_feet")),
                lat=it.get("lat"), lng=it.get("lng"), listing_url="",
                photos=[f"https://img.zumpercdn.com/{i}/1280x960" for i in (it.get("image_ids") or [])],
                manager=it.get("brokerage_name") or "", available=date_only(it.get("date_available")),
                amenities=amen, fenced_yard=True if "Fenced yard" in amen else None,
                pet_notes=("Zumper pets: " + ", ".join(map(str, pets))) if pets else "",
                source="Zumper (discovery only; verify on another site)"))
            out[-1]["zumper_url"] = ("https://www.zumper.com" + it["url"]) if it.get("url") else ""
    return out


SOURCES = [  # order = priority when the same home appears in several sources
    ("Zillow", src_zillow), ("Trulia", src_trulia), ("Redfin", src_redfin), ("Invitation Homes", src_invitation),
    ("Progress Residential", src_progress), ("AMH", src_amh), ("Tricon Residential", src_tricon),
    ("AppFolio", src_appfolio), ("Rentable", src_rentable), ("Craigslist", src_craigslist), ("Zumper", src_zumper),
]
SKIPPED = {"Realtor.com": "HTTP 429 bot block", "HotPads": "client stream only; same Zillow inventory",
           "FirstKey Homes": "client-rendered, no public JSON"}


# ----------------------------------------------------------------------------- main
def load_homes(path):
    with open(path, "rb") as f:
        raw = f.read()
    return raw, json.loads(raw.decode("utf-8"))


def build_ctx(homes):
    seen_addr, seen_url, subs, mgr = set(), set(), set(), {}
    for h in homes:
        if h.get("address"):
            seen_addr.add(norm(h["address"]))
            full, base, has_unit = addr_keys(h["address"])
            st, z = base.split("|")
            seen_addr.update({full, f"{st}#{full.split('#')[1].split('|')[0]}|", f"{st}|{z}", f"{st}|"})
            if not has_unit:
                seen_addr.update({f"{st}|{z}/nounit", f"{st}|/nounit"})
        for k in ("listing_url", "zumper_url", "apply_url"):
            u = h.get(k)
            if u:
                seen_url.add(u)
                seen_url.add(u.rstrip("/"))
        u = h.get("listing_url") or ""
        m = re.match(r'https?://([a-z0-9-]+)\.appfolio\.com/', u)
        if m:
            subs.add(m.group(1))
            if h.get("manager"):
                mgr.setdefault(m.group(1), {}).setdefault(h["manager"], 0)
                mgr[m.group(1)][h["manager"]] += 1
    return {"seen_addr": seen_addr, "seen_url": seen_url, "appfolio_subs": subs,
            "appfolio_mgr": {s: max(c, key=c.get) for s, c in mgr.items()}, "notes": {}, "complete": {}}


def cand_keys(address):
    """Every dedupe key an address answers to (same street+unit, or street alone when either side has no unit)."""
    full, base, has_unit = addr_keys(address)
    st, z = base.split("|")
    unit = full.split("#")[1].split("|")[0]
    cands = {norm(address), full, f"{st}#{unit}|"}
    if has_unit:
        cands |= {f"{st}|{z}/nounit", f"{st}|/nounit"}
    else:
        cands |= {f"{st}|{z}", f"{st}|"}
    return cands


SRC_HOSTS = {"Zillow": "zillow.com", "Redfin": "redfin.com", "Invitation Homes": "invitationhomes.com",
             "Progress Residential": "rentprogress.com", "AMH": "amh.com", "Tricon Residential": "triconresidential.com",
             "Zumper": "zumper.com", "Rentable": "rentable.co", "Craigslist": "craigslist.org", "Trulia": "trulia.com"}


# sources whose read is the whole inventory (the manager feeds only list a page or a few homes, so they confirm but never say "gone")
ENUMERATING = {"Zillow", "Trulia", "Redfin", "AppFolio"}
MIN_FULL = {"Zillow": 1500, "Trulia": 1500, "Redfin": 1500, "AppFolio": 100}   # fewer found than this = a partial read


def home_sources(h):
    """Which Live Sync sources list this home (by its source names and the hosts of its links)."""
    out = set()
    srcs = (h.get("source") or "")
    urls = [h.get("listing_url") or "", h.get("zillow_url") or "", h.get("zumper_url") or ""] + list((h.get("links") or {}).values())
    for name, host in SRC_HOSTS.items():
        if name.lower() in srcs.lower() or any(host in (u or "") for u in urls):
            out.add(name)
    if any("appfolio.com" in (u or "") for u in urls):
        out.add("AppFolio")
    if "Zillow" in out:
        out.add("Trulia")   # Trulia lists Zillow's rental inventory
    return out


def verify_homes(homes, results, status, ctx, today):
    """Re-check every shown home against today's full inventories. Seen anywhere -> confirmed live today.
    Not seen, while a source that lists it was read in full today -> 'not_seen' (never hidden, never invented)."""
    seen_keys, seen_urls, seen_by = set(), set(), {}
    for name, recs in results.items():
        for r in recs:
            ks = cand_keys(r["address"]) if r.get("address") else set()
            seen_keys |= ks
            for k in ks:
                seen_by.setdefault(k, name)
            for u in (r.get("listing_url"), r.get("zumper_url")):
                if u:
                    seen_urls.add(u.rstrip("/"))
    # only sources that list their whole inventory can say a home is gone
    complete = {n for n in ENUMERATING if status.get(n, {}).get("ok") and ctx["complete"].get(n, True)
                and status[n].get("found", 0) >= MIN_FULL.get(n, 0)}
    counts = {"confirmed": 0, "not_seen": 0, "unchanged": 0}
    for h in homes:
        if h.get("hidden") or not h.get("address"):
            continue
        ks = cand_keys(h["address"])
        urls = {(h.get(k) or "").rstrip("/") for k in ("listing_url", "zillow_url", "zumper_url")} - {""}
        hit = ks & seen_keys
        src = seen_by[next(iter(hit))] if hit else next((n for n in SRC_HOSTS if any(SRC_HOSTS[n] in u for u in urls & seen_urls)), None)
        if hit or (urls & seen_urls):
            h["avail_check"] = {"date": today, "verdict": "confirmed", "summary": f"Live: found on {src} by Live Sync {today}", "source": "live_sync"}
            counts["confirmed"] += 1
            continue
        covered = home_sources(h) & complete
        prev = (h.get("avail_check") or {})
        if covered and prev.get("verdict") == "confirmed" and prev.get("date") == today:
            counts["unchanged"] += 1      # already seen live today by a fuller run; a thinner one cannot undo that
            continue
        if covered:
            last = prev.get("date") if prev.get("verdict") == "confirmed" else (prev.get("last_confirmed") if prev else None)
            h["avail_check"] = {"date": today, "verdict": "not_seen",
                                "summary": f"Not seen on {', '.join(sorted(covered))} by Live Sync {today}" + (f"; last confirmed {last}" if last else "") + " — check the listing before visiting",
                                "last_confirmed": last, "source": "live_sync"}
            counts["not_seen"] += 1
        else:
            counts["unchanged"] += 1
    return counts


def merge(a, b):
    """Fill empty fields of a from b; union photos; combine source names."""
    for k in FIELDS:
        if k in ("source", "id", "checked"):
            continue
        if a.get(k) in (None, "", []) and b.get(k) not in (None, "", []):
            a[k] = b[k]
    if len(b.get("photos") or []) > len(a.get("photos") or []):
        a["photos"], a["photo_count_listing"] = b["photos"], b.get("photo_count_listing")
    srcs = [s for s in a["source"].split(", ")] + [s for s in b["source"].split(", ") if s not in a["source"]]
    a["source"] = ", ".join(srcs)
    if not a.get("listing_url") and b.get("listing_url"):
        a["listing_url"] = b["listing_url"]
    if b.get("zumper_url") and not a.get("zumper_url"):
        a["zumper_url"] = b["zumper_url"]
    return a


def make_id(r, existing):
    base = f"{ID_PREFIX.get(r['city'], slug(r['city']))}-{slug(r['name'])}"
    cand, i = base, 2
    while cand in existing:
        cand, i = f"{base}-{i}", i + 1
    existing.add(cand)
    return cand


def run(homes_path, write, log_path):
    raw, homes = load_homes(homes_path)
    ctx = build_ctx(homes)
    results, status = {}, {}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fn, ctx): name for name, fn in SOURCES}
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                recs = fut.result()
                results[name] = recs
                status[name] = {"ok": True, "found": len(recs), "error": None}
            except Exception as e:  # a failing source is reported, never fatal
                results[name] = []
                status[name] = {"ok": False, "found": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            if ctx["notes"].get(name):
                status[name]["note"] = ctx["notes"][name]
    for name, why in SKIPPED.items():
        status[name] = {"ok": False, "found": 0, "error": "skipped: " + why}

    # ---- filter + dedupe
    new_by_key, dropped = {}, {"in_homes": 0, "bad": 0, "merged": 0}
    for name, _ in SOURCES:
        for r in results.get(name, []):
            if r.get("city") not in CITIES or r.get("type") not in TYPES or not is_street(r.get("name")):
                dropped["bad"] += 1
                continue
            if r.get("rent_min") is not None and not (500 <= r["rent_min"] <= 20000):
                dropped["bad"] += 1      # placeholder prices like $0 / $1
                continue
            if "zumper.com" in (r.get("listing_url") or ""):
                r["zumper_url"], r["listing_url"] = r["listing_url"], ""
            full, base, has_unit = addr_keys(r["address"])
            key = full
            st, z = base.split("|")
            unit = full.split("#")[1].split("|")[0]
            # same street+unit (zip optional); or either side has no unit -> street (+zip) alone
            cands = {norm(r["address"]), full, f"{st}#{unit}|"}
            if has_unit:
                cands |= {f"{st}|{z}/nounit", f"{st}|/nounit"}
            else:
                cands |= {f"{st}|{z}", f"{st}|"}
            urls = {u for u in (r.get("listing_url"), r.get("zumper_url")) if u}
            if (cands & ctx["seen_addr"]) or any(u in ctx["seen_url"] or u.rstrip("/") in ctx["seen_url"] for u in urls):
                dropped["in_homes"] += 1
                continue
            if key in new_by_key:
                merge(new_by_key[key], r)
                dropped["merged"] += 1
            else:
                new_by_key[key] = r
    ids = {h.get("id") for h in homes}
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    new = []
    for r in new_by_key.values():
        r["checked"] = today
        r["id"] = make_id(r, ids)
        r["new_since"] = now
        r["avail_check"] = {"date": today, "verdict": "confirmed", "summary": f"Live: found on {r['source']} by Live Sync {today}", "source": "live_sync"}
        for k in NEVER_WRITE:
            r.pop(k, None)
        new.append(r)

    verified = verify_homes(homes, results, status, ctx, today)
    summary = {"ran_at": now, "sources": status, "candidates": sum(len(v) for v in results.values()),
               "dropped": dropped, "new": len(new), "verified": verified,
               "homes_total": len(homes) + (len(new) if write else 0),
               "added": [r["id"] for r in new] if write else [], "wrote": bool(write)}

    if write:
        if not isinstance(homes, list):
            raise RuntimeError("homes.json is not a list")
        homes.extend(new)
        tmp = homes_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(homes, f, separators=(",", ":"), ensure_ascii=True)
        json.loads(open(tmp, "rb").read())  # sanity: still valid JSON
        import os
        os.replace(tmp, homes_path)
    if log_path:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
            f.write("\n")

    print(f"live_sync: {summary['candidates']} candidates, {len(new)} new, verified {verified} "
          f"(dropped {dropped}) in {time.monotonic()-T0:.0f}s", file=sys.stderr)
    for name in list(dict(SOURCES)) + list(SKIPPED):
        s = status[name]
        print(f"  {'OK ' if s['ok'] else 'ERR'} {name:22s} {s['found']:4d}  {s.get('error') or ''} {s.get('note') or ''}", file=sys.stderr)
    if not write:
        print(json.dumps({"summary": summary, "new": new}, indent=1))
    else:
        print(json.dumps(summary, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("homes")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--log")
    a = ap.parse_args(argv)
    try:
        return run(a.homes, a.write, a.log)
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
