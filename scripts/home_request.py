"""Apply one request sent from a listing on the Nova Homes page (workflow home-request.yml).

usage: KIND=... HOME_ID=... DATA='{"..."}' python3 home_request.py homes.json data/requests.json
KIND  rented    Josh says it is rented: hidden='rented' + rented_at (Undo in the Passed on tab sends 'unrented')
      unrented  puts it back (only clears hidden when the reason was 'rented')
      facts     Josh's own marks, DATA={"facts":{"private_pool":true,"basement":null,...}}; stored in facts_josh,
                which the page lays over the checked facts (null removes a mark)
      sync      "this listing is incomplete": DATA={"wants":[...],"note":"..."}; sets sync_req on the home
                and queues it in data/requests.json for Claude to look at
Every request is also logged in data/requests.json (status 'open' for sync, 'applied' for the rest).
"""
import json, os, sys, time
from pathlib import Path

FACTS = {"private_pool", "single_level", "basement", "ground_living", "updated_interior", "dog_ground",
         "private_yard", "fenced_yard", "garage", "in_unit_wd", "community_pool", "gated"}
WANTS = {"photos", "description", "renovated", "pool", "yard", "stories", "details", "other"}


def main():
    homes_path, req_path = Path(sys.argv[1]), Path(sys.argv[2])
    kind, hid = os.environ.get("KIND", ""), os.environ.get("HOME_ID", "")
    data = json.loads(os.environ.get("DATA") or "{}")
    if kind not in ("rented", "unrented", "facts", "sync"):
        sys.exit(f"unknown kind {kind!r}")
    homes = json.loads(homes_path.read_text())
    h = next((x for x in homes if str(x.get("id")) == hid), None)
    if h is None:
        sys.exit(f"no home with id {hid!r}")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"at": now, "kind": kind, "id": hid, "address": h.get("address") or h.get("name"), "status": "applied"}

    if kind == "rented":
        if not h.get("hidden"):
            h["hidden"] = "rented"
        h["rented_at"] = now
    elif kind == "unrented":
        if h.get("hidden") == "rented":
            del h["hidden"]
        h.pop("rented_at", None)
    elif kind == "facts":
        marks = dict(h.get("facts_josh") or {})
        for k, v in (data.get("facts") or {}).items():
            if k not in FACTS or v not in (True, False, None):
                continue
            if v is None:
                marks.pop(k, None)
            else:
                marks[k] = v
        if marks:
            h["facts_josh"], h["facts_josh_at"] = marks, now
        else:
            h.pop("facts_josh", None); h.pop("facts_josh_at", None)
        entry["facts"] = marks
    else:
        wants = [w for w in data.get("wants") or [] if w in WANTS]
        note = str(data.get("note") or "")[:1000]
        h["sync_req"] = {"at": now, "wants": wants, "note": note}
        entry.update(wants=wants, note=note, status="open", listing_url=h.get("listing_url"))

    tmp = homes_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(homes, separators=(",", ":"), ensure_ascii=True))
    tmp.replace(homes_path)
    log = json.loads(req_path.read_text()) if req_path.exists() else []
    log.append(entry)
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_path.write_text(json.dumps(log, indent=1))
    print(json.dumps(entry))


if __name__ == "__main__":
    main()
