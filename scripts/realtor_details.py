"""Fill homes found only on Realtor.com with their live rental page: full photo set, description, MLS features (2026-11-02).

usage: python3 -I realtor_details.py <homes.json> <realtor-details.json> [--write]
realtor-details.json = {home id: {st, txt, det, pet, ph:[rdcpix photo keys]}} read in Josh's Chrome from
https://www.realtor.com/rentals/details/<slug> (the built-in browser cannot open Realtor.com).
Writes: photos (rdcpix 1024x768, hotlinked like Zillow photos), notes (first ~600 chars of the description when the
home has none), listing_text (description + features, read by facts.py), mls {stories, pool_private, landscape_back}.
Never touches status/notes Josh wrote, facts_josh, or anything else.
"""
import json, re, sys

def parse_det(det):
    kv = {}
    for part in (det or "").split(" | "):
        if ": " not in part:
            continue
        for item in part.split(": ", 1)[1].split("; "):
            if ":" in item:
                k, v = item.split(":", 1)
                kv.setdefault(k.strip().lower(), v.strip())
    return kv

def short(txt, n=600):
    txt = re.sub(r"\s+", " ", txt or "").strip()
    if len(txt) <= n:
        return txt
    cut = txt[:n]
    i = max(cut.rfind(". "), cut.rfind("! "))
    return (cut[:i + 1] if i > 200 else cut.rstrip() + "…")

def main():
    hp, dp = sys.argv[1], sys.argv[2]
    write = "--write" in sys.argv
    homes = json.load(open(hp)); D = json.load(open(dp))
    byid = {h["id"]: h for h in homes}
    n = {"photos": 0, "notes": 0, "stories": 0, "pool": 0, "land": 0, "missing": 0, "err": 0}
    for hid, v in D.items():
        h = byid.get(hid)
        if not h:
            n["missing"] += 1; continue
        if v.get("err"):
            n["err"] += 1; continue
        ph = [f"https://ap.rdcpix.com/{k[:-1]}od-w1024_h768.jpg" for k in v.get("ph") or [] if re.fullmatch(r"[0-9a-f]{32}[a-z]-[a-z0-9]+", k)]
        if len(ph) > len(h.get("photos") or []):
            h["photos"], h["photo_count_listing"] = ph, len(ph); n["photos"] += 1
        if v.get("txt") and not h.get("notes"):
            h["notes"] = short(v["txt"]); n["notes"] += 1
        h["listing_text"] = ((v.get("txt") or "") + " | " + (v.get("det") or "")).strip(" |")
        kv = parse_det(v.get("det"))
        mls = {}
        st = kv.get("levels or stories") or kv.get("building total stories") or kv.get("levels")
        if st and re.fullmatch(r"\d+(\.\d+)?", st):
            mls["stories"] = st; n["stories"] += 1
        pp = kv.get("pool private")
        if pp in ("Yes", "No"):
            mls["pool_private"] = pp == "Yes"; n["pool"] += 1
        land = kv.get("landscaping") or ""
        back = ", ".join(x.strip() for x in land.split(",") if "back" in x.lower())
        if back:
            mls["landscape_back"] = back; n["land"] += 1
        try:
            pet = json.loads(v.get("pet") or "null") or {}
        except ValueError:
            pet = {}
        if pet.get("dogs") is True and (h.get("dogs") in (None, "", "Unknown")):
            h["dogs"] = "Allowed"; h["pet_notes"] = (h.get("pet_notes") or "Realtor.com listing: dogs allowed"); n["dogs"] = n.get("dogs", 0) + 1
        if mls:
            mls["source"] = "Realtor.com rental listing 2026-11-02"
            h["mls"] = mls
    print(json.dumps(n))
    if write:
        tmp = hp + ".tmp"
        json.dump(homes, open(tmp, "w"), separators=(",", ":"), ensure_ascii=True)
        __import__("os").replace(tmp, hp)

if __name__ == "__main__":
    main()
