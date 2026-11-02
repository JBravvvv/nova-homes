"""Checked facts behind the must-have filters (2026-10-30).

Josh: every filter must be exactly right — a home with only a community pool must not pass the pool
filter, and no home with its own pool may be lost; same for single-level and the rest. The scraped
amenity words are not reliable enough for that, so this script writes a `facts` object per home that
the page prefers over the amenity list:
  private_pool, community_pool, single_level, garage, fenced_yard, private_yard, in_unit_wd, gated, basement,
  updated_interior, dog_ground (grass/rock area besides the pool), ground_living (single level, basement, or main-floor primary) — added 2026-11-01
(basement added 2026-10-30: Zillow's "Has basement: Yes/No" fact, else the listing text; a basement the owner keeps does not count)
Each is true, false or null (unknown), decided from (in order) Zillow's own structured fields read from
the home's rental page, then the listing text (notes/description, amenity words) with wording that tells
a private pool from a community one. Nothing is invented: when no source says either way, it stays null
and the page falls back to the amenity words. `facts_why` keeps the evidence per field.

usage: facts.py <homes.json> <snapshot homes dir> <zx.json> [<zx2.json> ...] [--redfin=<file>] [--write]
"""
import json, sys, re, time
from pathlib import Path
args=[a for a in sys.argv[1:] if a!='--write' and not a.startswith('--redfin=')]; WRITE='--write' in sys.argv
# --redfin=<file>: {home id: {stories: '1'|'2'|..., url}} read from Redfin's public-record facts (2026-11-01)
RF={}
for a in sys.argv[1:]:
    if a.startswith('--redfin='): RF.update({k:v for k,v in json.load(open(a.split('=',1)[1])).items() if v.get('stories')})
hp, snap = args[0], Path(args[1])
zx={}
for f in args[2:]:
    for k,v in json.load(open(f)).items():
        if v and not v.get('err') and not v.get('blocked'): zx.setdefault(k,{}).update({a:b for a,b in v.items() if b not in (None,'',[])})
d=json.load(open(hp)); homes=d if isinstance(d,list) else d['homes']
def zmatch(h,z):
    num=(h.get('address') or ' ').split()[0]; zips=re.findall(r'\b(\d{5})\b',h.get('address') or ''); zu=z.get('url') or ''
    return '/homedetails/' in zu and num in zu and (not zips or zips[-1] in zu) and z.get('st')=='FOR_RENT'
COMM=re.compile(r"community[^.;]{0,30}\bpools?\b|resort[- ]style pool|shared pool|hoa pool|association pool|\bpools?, (playground|tennis|fitness|spa|gym|clubhouse)|community (with|has|offers|features|amenities)[^.;]{0,40}\bpool|access to (the |a )?(community |resort )?pool|(heated )?community (lap )?pool|neighborhood pool|complex pool|clubhouse[^.;]{0,30}pool|(walking paths?|lakeside|fitness center|business center|tennis courts?)[^.;]{0,60}\bpools?\b|\bpools?\b[^.;]{0,60}(walking paths?|lakeside|fitness center|business center|tennis courts?)",re.I)
PRIV=re.compile(r"private (sparkling |heated |pebble[- ]tec |play |diving |lap |salt ?water |fenced )?pool|own (private )?pool|backyard (features |with |has |boasts )?(a |an )?(sparkling |heated |pebble[- ]tec |play |diving |refreshing |beautiful |large |resort[- ]style |fenced |salt ?water )*pool|pool in (the |your )?(back ?yard|rear)|\bpool\b[^.;]{0,25}(back ?yard|in the yard)|sparkling (pool|pebble[- ]tec pool)|pebble[- ]tec pool|diving pool|play pool|pool (and|&|with) (spa|hot tub|jacuzzi|waterfall|slide)|pool service (included|provided|paid)|pool (maintenance|care) (included|provided|is included)|heated pool|salt ?water pool|fenced pool|pool fence|dive[- ]in pool|home (with|has|features|boasts|offers)[^.;]{0,40}\bpool\b",re.I)
# wording that can only mean the home's own pool (a community pool can also be "heated", "sparkling", "pool and spa")
STRONG=re.compile(r"private (sparkling |heated |pebble[- ]tec |play |diving |lap |salt ?water |fenced )?pool|own (private )?pool|backyard (features |with |has |boasts |includes )?(a |an |your )?(sparkling |heated |pebble[- ]tec |play |diving |refreshing |beautiful |large |resort[- ]style |fenced |salt ?water |private )*pool|pool in (the |your )?(back ?yard|rear)|\bpool\b[^.;]{0,25}(back ?yard|in the yard)|pool service (included|provided|paid)|pool (maintenance|care) (included|provided|is included)|fenced pool|pool fence|home (with|has|features|boasts|offers)[^.;]{0,40}\bpool\b",re.I)
COMM_LITERAL=re.compile(r"community[^.;]{0,15}\bpools?\b|hoa pool|shared pool|neighborhood pool|complex pool|association pool|resort[- ]style pool",re.I)
NOPOOL=re.compile(r"\bno pool\b|without a pool|pool is not",re.I)
SINGLE=re.compile(r"single[- ](level|story|storey)|one[- ](level|story|storey)|\b1[- ](level|story|storey)|all on one (level|floor)|no stairs|ranch[- ]style",re.I)
MULTI=re.compile(r"two[- ](level|story|storey)|2[- ](level|story|storey)|tri[- ]level|three[- ](level|story)|3[- ]story|split[- ]level|upstairs|second (floor|level|story)|downstairs|\bloft\b|stairs",re.I)
WD=re.compile(r"washer[^.;]{0,12}dryer|w/d (included|in unit|in-unit|hookups? and)|in[- ]unit (laundry|washer)|laundry (in unit|in-unit|room with washer)|full[- ]size washer|washer and dryer",re.I)
WDHOOK=re.compile(r"w/d hook ?ups? only|hook ?ups? only|washer[^.;]{0,10}dryer hook",re.I)
GATED=re.compile(r"\bgated (community|entry|entrance|neighborhood|complex|subdivision)|guard[- ]gated|behind (the |a )?gates?",re.I)
FENCE=re.compile(r"(block|fully|completely|fenced|private|enclosed)[- ]?(wall|fenced|fence)|fenced[- ](in |back)?yard|back ?yard[^.;]{0,20}(block|fence|wall)|\bfenced\b",re.I)
NOFENCE=re.compile(r"not fenced|no fence|unfenced|without a fence",re.I)
YARD=re.compile(r"\b(back ?yard|private yard|rear yard|fenced yard|yard)\b",re.I)
BASE=re.compile(r"\bbasements?\b",re.I)
NOBASE=re.compile(r"\bno basement|without (a )?basement|basement[- ]free|not have a basement",re.I)
BASE_TEMPLATE=re.compile(r"in basement washer|washer[^.;]{0,20}in basement",re.I)   # templated laundry line, not a basement claim
BASE_OWNER=re.compile(r"basement[^;]{0,120}retained by (the )?owner",re.I)
UPD=re.compile(r"\b(fully |completely |beautifully |newly |recently |tastefully |nicely |extensively |totally |freshly )?(updated|remodel(l)?ed|renovated|upgraded|rehabbed|modernized)\b|\b(a |beautiful |stunning |complete |full |gorgeous )remodel\b|\bmodern (finishes|kitchen|upgrades|design|interior)|designer (finishes|touches)|\bquartz\b|waterfall (island|edge)|shaker cabinet|new (kitchen|cabinets|cabinetry|countertops|flooring|floors|vinyl|lvp|appliances)|luxury vinyl|\blvp\b",re.I)
UPD_NEG=re.compile(r"\b(original|dated|older) (kitchen|cabinets|finishes|interior|bathrooms?|condition)|\bas[- ]is\b|fixer|needs (updating|work|tlc)",re.I)
UPD_WEAK=re.compile(r"granite|stainless|fresh(ly)? paint|new paint|new carpet|tile (floor|flooring|throughout)|wood[- ]look tile|white cabinet|farmhouse sink|tile backsplash|plank (tile|flooring)",re.I)
GROUND=re.compile(r"(grass(y)?|lawn|turf|sod)\b|desert landscap|rock (yard|landscap|area)|\bgravel\b|landscaped (back ?)?yard|xeriscap|(large|big|spacious|huge|ample|plenty of|room for|lots of) (back ?)?(yard|grass|lawn)|(yard|grass) (space|area) for|dog run",re.I)
GROUND_NEG=re.compile(r"no (grass|lawn|yard space)|(all|entirely|fully) (hardscape|concrete|pavers)|pool (takes up|fills) (the |most of the |all of the )?(back)?yard",re.I)
MAINFLOOR=re.compile(r"(primary|master|owner'?s?)( bedroom| suite| bed| retreat)?[^.;]{0,25}(downstairs|on the (main|first|ground) (floor|level)|main[- ](floor|level)|first[- ]floor|ground[- ](floor|level))|(downstairs|main[- ]level|first[- ]floor|ground[- ]floor) (primary|master|owner)|(only|just) (a |the |one )?(bonus room|loft|game room|bedroom|office) (is )?upstairs|upstairs (is )?(only )?(a |the )?(bonus room|loft|game room)",re.I)
GARTXT=re.compile(r"\b(1|2|3|4|one|two|three|four)[- ]car garage|garage spaces?:\s*[1-9]|attached garage|garage parking",re.I)   # explicit wording beats Zillow's rental default hasGarage=false
NOYARD=re.compile(r"no (back ?yard|yard|private yard|real yard)|without a (back ?)?yard|shared yard|common area only",re.I)
changed=0
for h in homes:
    if h.get('hidden'): continue
    am=set(h.get('amenities') or []); txt=' '.join(str(h.get(k) or '') for k in ('notes','specials','name','listing_text')); z=zx.get(h['id']); zt=z if z and zmatch(h,z) else None
    if zt and zt.get('desc'): txt+=' '+zt['desc']
    f={}; why={}
    # private pool — wording that names the home's own pool always wins, so no real pool is lost; Zillow's "no private pool" is used only when nothing in the text claims one
    pm=PRIV.search(txt)
    sm=next((m for m in STRONG.finditer(txt) if 'communit' not in (txt[max(0,m.start()-40):m.end()]).lower()),None)
    npool=len(re.findall(r"\bpools?\b",txt,re.I)); ncomm=len(COMM.findall(txt))
    mls=h.get('mls') or {}   # structured MLS fields from the home's live Realtor.com rental page (scripts/realtor_details.py, 2026-11-02)
    if mls.get('pool_private') is not None: f['private_pool']=mls['pool_private']; why['private_pool']='Realtor.com listing: Pool Private: '+('Yes' if mls['pool_private'] else 'No')
    elif NOPOOL.search(txt): f['private_pool']=False; why['private_pool']='text: no pool'
    elif npool and ncomm>=npool and not (zt and zt.get('pool')=='true'): f['private_pool']=False; why['private_pool']='text: every pool mention is the community pool'
    elif sm: f['private_pool']=True; why['private_pool']='text: '+sm.group(0)
    elif COMM.search(txt) and not (zt and zt.get('pool')=='true'): f['private_pool']=False; why['private_pool']='text names a community pool and nothing says the home has its own'
    elif pm and not COMM.search(txt): f['private_pool']=True; why['private_pool']='text: '+pm.group(0)
    elif COMM.search(txt) and zt and zt.get('pool')=='true' and not COMM_LITERAL.search(txt): f['private_pool']=True; why['private_pool']='Zillow: has private pool (text mentions the community but never a community pool)'
    elif COMM.search(txt): f['private_pool']=False; why['private_pool']='text names only a community pool'+(' (Zillow flags a private pool, but the listing says community pool)' if zt and zt.get('pool')=='true' else '')
    elif zt and zt.get('pool')=='true': f['private_pool']=True; why['private_pool']='Zillow: has private pool'
    elif 'Private pool' in am and not COMM.search(txt): f['private_pool']=True; why['private_pool']='amenity: Private pool'
    elif zt and zt.get('pool')=='false' and (COMM.search(txt) or not re.search(r"\bpool\b",txt,re.I)): f['private_pool']=False; why['private_pool']='Zillow: no private pool'+(' (text mentions only the community pool)' if COMM.search(txt) else '')
    elif COMM.search(txt) and len(re.findall(r"\bpools?\b",txt,re.I))<=1: f['private_pool']=False; why['private_pool']='text: only a community pool is mentioned'
    elif 'Pool' in am and not COMM.search(txt): f['private_pool']=None; why['private_pool']='amenity says Pool but nothing confirms it is the home\'s own: not counted'
    elif 'Pool' in am and COMM.search(txt): f['private_pool']=None; why['private_pool']='amenity Pool but text also names a community pool: unclear'
    else: f['private_pool']=None
    # community pool
    if COMM.search(txt) or 'Community pool' in am: f['community_pool']=True; why['community_pool']='text/amenity: community pool'
    else: f['community_pool']=None
    # single level
    st=zt and zt.get('stories'); lv=(zt and zt.get('levels') or '').lower()
    if st: f['single_level']=float(st)<=1; why['single_level']=f'Zillow: {st} stories'
    elif lv: f['single_level']=lv.startswith('one') or lv=='1'; why['single_level']='Zillow levels: '+lv
    elif mls.get('stories'): f['single_level']=float(mls['stories'])<=1; why['single_level']=f"Realtor.com listing: {mls['stories']} stories"
    elif h['id'] in RF: f['single_level']=float(RF[h['id']]['stories'])<=1; why['single_level']=f"Redfin public record: {RF[h['id']]['stories']} stories"
    elif SINGLE.search(txt) and not MULTI.search(txt): f['single_level']=True; why['single_level']='text: '+SINGLE.search(txt).group(0)
    elif MULTI.search(txt) and not SINGLE.search(txt): f['single_level']=False; why['single_level']='text: '+MULTI.search(txt).group(0)
    elif 'Single-level' in am: f['single_level']=True; why['single_level']='amenity: Single-level'
    else: f['single_level']=None
    # garage
    g=h.get('garage_spaces')
    if zt and zt.get('gar') not in (None,'','0'): f['garage']=True; why['garage']=f"Zillow: {zt['gar']}-car garage"
    elif GARTXT.search(txt) and not re.search(r"no garage|carport only",txt,re.I): f['garage']=True; why['garage']='text: '+GARTXT.search(txt).group(0)
    elif zt and (zt.get('hg')=='false' or (zt.get('gar')=='0')): f['garage']=False; why['garage']='Zillow: no garage'
    elif zt and zt.get('park') and not any('garage' in p.lower() for p in zt['park']) and any(p.lower() in ('carport','open','none','assigned','street') or 'carport' in p.lower() for p in zt['park']): f['garage']=False; why['garage']='Zillow parking: '+', '.join(zt['park'])
    elif isinstance(g,(int,float)) and g>0: f['garage']=True; why['garage']=f'listing: {g} garage spaces'
    elif re.search(r"\b(1|2|3|one|two|three)[- ]car garage|\bgarage\b",txt,re.I) and not re.search(r"no garage|carport only",txt,re.I): f['garage']=True; why['garage']='text: garage'
    elif re.search(r"no garage|carport only|carport \(no garage\)",txt,re.I): f['garage']=False; why['garage']='text: no garage'
    elif 'Garage' in am: f['garage']=True; why['garage']='amenity: Garage'
    else: f['garage']=None
    # fenced yard / private yard
    fence=(zt and zt.get('fence') or '').lower()
    if fence and fence not in ('none','null','false'): f['fenced_yard']=True; why['fenced_yard']='Zillow fencing: '+fence
    elif fence in ('none',): f['fenced_yard']=False; why['fenced_yard']='Zillow fencing: none'
    elif h.get('fenced_yard') is True: f['fenced_yard']=True; why['fenced_yard']='listing: fenced yard'
    elif NOFENCE.search(txt): f['fenced_yard']=False; why['fenced_yard']='text: not fenced'
    elif FENCE.search(txt): f['fenced_yard']=True; why['fenced_yard']='text: '+FENCE.search(txt).group(0)
    elif 'Fenced yard' in am: f['fenced_yard']=True; why['fenced_yard']='amenity: Fenced yard'
    else: f['fenced_yard']=None
    if f['fenced_yard']: f['private_yard']=True; why['private_yard']='fenced yard'
    elif NOYARD.search(txt): f['private_yard']=False; why['private_yard']='text: '+NOYARD.search(txt).group(0)
    elif YARD.search(txt) or 'Private yard' in am: f['private_yard']=True; why['private_yard']='text/amenity: yard'
    else: f['private_yard']=None
    # in-unit washer/dryer
    la=[x.lower() for x in (zt and zt.get('laundry') or [])]; ap=[x.lower() for x in (zt and zt.get('appl') or [])]
    if any('in unit' in x or 'inside' in x or 'in-unit' in x or 'laundry room' in x for x in la) or ('washer' in ap and 'dryer' in ap): f['in_unit_wd']=True; why['in_unit_wd']='Zillow: '+', '.join(la or ap)
    elif la and any('hookup' in x for x in la) and not any('in unit' in x for x in la) and not ('washer' in ap): f['in_unit_wd']=False; why['in_unit_wd']='Zillow: hookups only'
    elif la and any('shared' in x or 'common' in x or 'on-site' in x or 'on site' in x for x in la): f['in_unit_wd']=False; why['in_unit_wd']='Zillow: shared laundry'
    elif WDHOOK.search(txt) and not WD.search(txt): f['in_unit_wd']=False; why['in_unit_wd']='text: hookups only'
    elif WD.search(txt): f['in_unit_wd']=True; why['in_unit_wd']='text: '+WD.search(txt).group(0)
    elif 'In-unit W/D' in am: f['in_unit_wd']=True; why['in_unit_wd']='amenity: In-unit W/D'
    else: f['in_unit_wd']=None
    # gated
    if GATED.search(txt) or 'Gated' in am: f['gated']=True; why['gated']='text/amenity: gated'
    else: f['gated']=None
    # basement — a property fact, so Zillow's page counts even when it is the for-sale page of the same address
    zb=z if z and (zt or ('/homedetails/' in (z.get('url') or '') and (h.get('address') or ' ').split()[0] in z.get('url',''))) else None
    if zb and zb.get('desc') and zb is not zt: txt+=' '+zb['desc']
    hb=(zb and zb.get('hasbsmt') or '').lower()
    if NOBASE.search(txt): f['basement']=False; why['basement']='text: '+NOBASE.search(txt).group(0)
    elif BASE_OWNER.search(txt): f['basement']=False; why['basement']='text: the basement is kept by the owner'
    elif hb=='yes': f['basement']=True; why['basement']='Zillow fact: Has basement: Yes'
    elif hb=='no': f['basement']=False; why['basement']='Zillow fact: Has basement: No'
    elif BASE.search(txt) and not (BASE_TEMPLATE.search(txt) and len(BASE.findall(txt))==1): f['basement']=True; why['basement']='text: '+re.search(r".{0,40}\bbasements?\b.{0,40}",txt,re.I).group(0).strip()
    else: f['basement']=None
    # updated interior (2026-11-01, Josh's ideal-home profile): explicit wording, or two or more modern-finish details
    um=UPD.search(txt); weak={m.group(0).lower() for m in UPD_WEAK.finditer(txt)}
    if UPD_NEG.search(txt) and not um: f['updated_interior']=False; why['updated_interior']='text: '+UPD_NEG.search(txt).group(0)
    elif um: f['updated_interior']=True; why['updated_interior']='text: '+um.group(0)
    elif len(weak)>=2: f['updated_interior']=True; why['updated_interior']='text: '+', '.join(sorted(weak))
    else: f['updated_interior']=None
    # grass or rock ground for Nova besides the pool (2026-11-01)
    gm=GROUND.search(txt)
    if GROUND_NEG.search(txt): f['dog_ground']=False; why['dog_ground']='text: '+GROUND_NEG.search(txt).group(0)
    elif re.search(r'grass|turf|gravel|stone|desert|rock',mls.get('landscape_back') or '',re.I): f['dog_ground']=True; why['dog_ground']='Realtor.com listing landscaping (back): '+mls['landscape_back']
    elif gm: f['dog_ground']=True; why['dog_ground']='text: '+gm.group(0)
    elif 'Desert landscaping' in am: f['dog_ground']=True; why['dog_ground']='amenity: Desert landscaping'
    else: f['dog_ground']=None
    # living mostly on the ground floor: single level, a basement, or main-floor primary with little upstairs (2026-11-01)
    mf=MAINFLOOR.search(txt)
    if f.get('single_level') is True: f['ground_living']=True; why['ground_living']='single level'
    elif f.get('basement') is True: f['ground_living']=True; why['ground_living']='has a basement (main living stays on one floor)'
    elif mf: f['ground_living']=True; why['ground_living']='text: '+mf.group(0)
    elif f.get('single_level') is False: f['ground_living']=False; why['ground_living']='two-story ('+why.get('single_level','')+')'
    else: f['ground_living']=None
    if h.get('facts')!=f:
        changed+=1
        if WRITE:
            h['facts']=f; h['facts_why']=why; h['facts_at']=time.strftime('%Y-%m-%d')
            sf=snap/f"{h['id']}.json"
            if sf.exists():
                for t in range(4):
                    try: s=json.loads(sf.read_text()); s['facts']=f; s['facts_why']=why; s['facts_at']=h['facts_at']; sf.write_text(json.dumps(s)); break
                    except Exception: time.sleep(2)
if WRITE: json.dump(d,open(hp,'w'),separators=(',',':'))
import collections
vis=[h for h in homes if not h.get('hidden')]
print('changed',changed)
for k in ('private_pool','community_pool','single_level','garage','fenced_yard','private_yard','in_unit_wd','gated','basement','updated_interior','dog_ground','ground_living'):
    c=collections.Counter((h.get('facts') or {}).get(k) if WRITE else None for h in vis); print(k,dict(c))
