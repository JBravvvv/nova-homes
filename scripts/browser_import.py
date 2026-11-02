"""Add homes read from Zillow / Realtor.com search results in Josh's Chrome (those sites block the GitHub runner and this Mac).

usage: python3 -I browser_import.py <homes.json> <dump.json> [--write] [--log <file>]
dump.json = {"zillow": [...], "realtor": [...]} in the compact shape the browser sweep writes (see CHANGELOG 2026-11-02).
Runs live_sync.run() with these two sources in place of the live ones, so filtering (area, type, $2,200 floor),
dedupe against homes.json, ids, new_since and "confirmed live" marks work exactly as in Live Sync. Neither source is
treated as a full inventory here, so a home missing from the dump is never marked not seen.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import live_sync as L

ZT = {"SINGLE_FAMILY": "House", "TOWNHOUSE": "Townhome", "MULTI_FAMILY": "Duplex"}
RT = {"single_family": "House", "townhomes": "Townhome", "townhouse": "Townhome", "duplex_triplex": "Duplex"}


def zillow_recs(items):
    out = []
    for z in items:
        typ = ZT.get(z.get("t") or "")
        street = z.get("s") or ""
        city = L.city_for(z.get("zp"), z.get("c"))
        if not typ or not city or not L.is_street(street):
            continue
        rmin = L.intnum(z.get("u"))
        rmax = rmin
        nums = [L.intnum(x) for x in __import__("re").findall(r'\$[\d,]+', z.get("p") or "")]
        nums = [n for n in nums if n]
        if nums and ("+" in (z.get("p") or "") or " - " in (z.get("p") or "")):
            rmin, rmax = min(nums), (max(nums) if len(nums) > 1 else None)
        photos = [f"https://photos.zillowstatic.com/fp/{k}-cc_ft_1536.jpg" for k in (z.get("ph") or []) if k]
        url = z.get("url") or ""
        if url.startswith("/"):
            url = "https://www.zillow.com" + url
        out.append(L.rec(name=street, address=z.get("a") or f"{street}, {z.get('c','')}, AZ {z.get('zp','')}",
                         city=city, type=typ, rent_min=rmin, rent_max=rmax, beds_min=L.intnum(z.get("b")),
                         baths=L.num(z.get("ba")), sqft_min=L.intnum(z.get("sf")), lat=z.get("la"), lng=z.get("lo"),
                         listing_url=url, photos=photos, manager=z.get("br") or "", available=L.date_only(z.get("av")),
                         source="Zillow"))
    return out


def realtor_recs(items):
    out = []
    for r in items:
        typ = RT.get(r.get("t") or "")
        street = r.get("line") or ""
        city = L.city_for(r.get("zip"), r.get("city"))
        if not typ or not city or not L.is_street(street):
            continue
        rmin = L.intnum(r.get("lp") or r.get("lpmin"))
        rmax = L.intnum(r.get("lpmax")) or rmin
        url = "https://www.realtor.com/rentals/details/" + r["href"] if r.get("href") else ""
        out.append(L.rec(name=street, address=f"{street}, {r.get('city','')}, AZ {r.get('zip','')}", city=city, type=typ,
                         rent_min=rmin, rent_max=rmax, beds_min=L.intnum(r.get("b")), baths=L.num(r.get("ba")),
                         sqft_min=L.intnum(r.get("sf")), lat=r.get("la"), lng=r.get("lo"), listing_url=url,
                         photos=[p for p in (r.get("ph") or []) if isinstance(p, str)], manager=r.get("adv") or "",
                         source="Realtor.com"))
    return out


def main():
    homes, dump = sys.argv[1], json.loads(Path(sys.argv[2]).read_text())
    rest = sys.argv[3:]
    write = "--write" in rest
    log = rest[rest.index("--log") + 1] if "--log" in rest else None

    def src(name, fn, key):
        def f(ctx):
            ctx["complete"][name] = False      # partial view: confirm only, never "not seen"
            return fn(dump.get(key) or [])
        return f
    L.SOURCES = [("Zillow", src("Zillow", zillow_recs, "zillow")), ("Realtor.com", src("Realtor.com", realtor_recs, "realtor"))]
    L.SKIPPED = {}
    return L.run(homes, write, log)


if __name__ == "__main__":
    sys.exit(main())
