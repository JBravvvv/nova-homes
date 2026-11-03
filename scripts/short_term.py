"""Short-term rental check for every home (2026-11-02, Josh: "rather one be on there that isn't
actually a short term rental than accidentally exclude one that is").

Each home gets `short_term = {verdict, why, at}`:
  yes     the listing offers a lease under 12 months, month-to-month, nightly/weekly/seasonal stays,
          or Zillow's lease-term field says so
  maybe   softer signs: furnished, flexible or negotiable lease terms
  no      the listing names only 12-month-or-longer terms, or rules short-term rentals out
  unknown no lease wording anywhere we have read
Josh's own "What it has" (facts_josh.short_term) always wins.

usage: short_term.py <homes.json> [--text=<json {id|url: text}> ...] [--zlease=<json {id: {lt,ld,fur,desc}}>] [--write]
"""
import json, re, sys, datetime

NUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
       'ten': 10, 'eleven': 11, 'twelve': 12, 'eighteen': 18, 'twenty-four': 24}
N = r'\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|eighteen|twenty-four)'
def n_of(s):
    s = s.lower()
    return int(s) if s.isdigit() else NUM.get(s, 99)

# Wording that rules short stays out, checked in the same sentence as a hit.
NEG_BEFORE = re.compile(r"\b(no|not|non|never|without|prohibit\w*|disallow\w*|cannot|can't|can ?not|doesn't|does not|won't|will not|isn't|aren't|nor|ban\w*)\b[^.;!\n]{0,40}$", re.I)
NEG_AFTER = re.compile(r"^[^.;!\n]{0,40}\b(not (allowed|permitted|accepted|available|considered|offered)|prohibited|are not|is not|forbidden|banned|strictly prohibited)\b", re.I)

STRONG = [
    (r'short[\s-]*term|short (or|and|/|-) ?(long|mid|extended)|short stays?|extended stays?', 'short-term'),
    (r'mid[\s-]*term', 'mid-term'),
    (r'month[\s-]*(to|2)[\s-]*month|\bm-?t-?m\b', 'month-to-month'),
    (r'(flexible|furnished|short) monthly|monthly (stays?|basis)\b|(rent(ed|s)?|available|lease[ds]?) (by the |on a )?month(ly)?\b(?! (free|off|of|rent))', 'monthly stays'),
    (r'vacation (rental|home|property|getaway)|\bair\s*b\s*n\s*b\b|\bvrbo\b|\bnightly\b|per night|/night\b|weekly (rate|rental|stays?)|(rent|rate|price)[^.;\n]{0,20}per week\b', 'vacation / nightly'),
    (r'\bseasonal(ly)?\b|\bsnow ?birds?\b|summer (rental|lease|months only)|(season(al)?|summer|winter|peak|off[\s-]*peak) (rates?|pricing|prices?|rent)|rates? (vary|varies|change)[^.;\n]{0,40}(season|duration|length of (the )?stay|time of year)|(for|open for) (the )?(20\d\d[-/]?(\d\d)? )?season\b|through(out)? the season|prices are for (summer|winter)|available for the months? of|available (only )?(from|between) (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w* (to|through|thru|-|until) (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|winter (rental|season|visitors?|getaway|months)|\bin season\b|off[\s-]*season', 'seasonal'),
    (r'corporate (housing|rental|lease|stays?|apartment)|executive (rental|housing)|travel(ing|ling)? (nurses?|professionals?)|temporary (housing|stay|rental)|insurance (housing|placement)', 'corporate / temporary'),
    (r'shorter[\s-]*(term )?(leases?|terms?|stays?|rentals?)[^.;\n]{0,30}(negotiable|considered|available|possible|ok|welcome|accepted|allowed)|(less|shorter) than (a |one |1 )?(year|12[\s-]*months?)[^.;\n]{0,20}(lease|term|stay|considered|negotiable|ok|available)|(lease|term|stay)s? (of )?(less|shorter) than (a |one |1 )?(year|12[\s-]*months?)', 'shorter lease considered'),
    (r'flexible (lease|leasing|terms?|length)|lease (terms? )?(are |is )?flexible|flex(ible)? lease|lease flexibility', 'flexible lease'),
]
# "6 month lease", "lease terms: 3, 6 or 12 months", "minimum 30 days", "3-11 months"
TERM = [
    re.compile(N + r'[\s-]*(?:to|-|–|or)?[\s-]*(?:' + N + r'[\s-]*)?(month|mo\.?|mos\.?)s?\b[\s-]*(\+\s*)?(lease|term|rental|stay|min|minimum|max|maximum|contract|option)', re.I),
    re.compile(r'(minimum|min\.?|at least|as short as|as little as)[\s:]*(lease|stay|rental|term|of)?[\s:]*(term|of|length|is)?[\s:]*' + N + r'[\s-]*(month|mo|night|day|week)s?', re.I),
    re.compile(r'lease (terms?|lengths?|options?|duration)[^.;\n]{0,50}?\b' + N + r'[\s-]*(month|mo)s?', re.I),
    re.compile(r'\b' + N + r'[\s-]*(night|day|week)s?[\s-]*(minimum|min\b|stay|rental)', re.I),
]
SOFT = [
    (r'\bgetaway\b|home stay|unforgettable stays?|(quick|weekend) (stay|trip)|business travel(ers?)?|fully stocked|between homes|stay a while|your (home|base) away from home|be our guest|guests? (will|can) (enjoy|love)|perfect (retreat|escape)', 'vacation-style wording'),
    (r'shorter leases?|(selected|chosen) lease term|length of (the )?lease|lease (length|term)s? (may )?var(y|ies)|longer lease (potentially )?available|rent varies (based on|by) (lease|term)', 'lease length varies'),
    (r'(?<!un)(?<!non-)(?<!not )\b(fully |partially |partly |beautifully |tastefully |completely )?furnished\b', 'furnished'),
    (r'negotiable (lease|terms?)|lease (terms? )?(are |is )?negotiable|(other|various|multiple|different) lease (terms?|lengths?|options?)|lease (terms?|lengths?) (vary|available|options)', 'lease terms negotiable'),
]
LONG_ONLY = re.compile(r'(\b(12|twelve|18|24)[\s-]*(month|mo)s?\b[\s-]*(\+\s*)?(minimum|min\b|lease|term)|(minimum|min\.?|at least)[\s:]*(lease|of|term)?[\s:]*(term|of)?[\s:]*(12|twelve|18|24)[\s-]*(month|mo)|\b(1|one)[\s-]*(year|yr)s?[\s-]*(\+\s*)?(minimum|min\b|lease)|no (short[\s-]*term|month[\s-]*to[\s-]*month|airbnb|vrbo|vacation)|short[\s-]*term (rentals? |leases? )?(are |is )?(not|prohibited|strictly))', re.I)


def sentence(t, i, j):
    a = max(t.rfind('.', 0, i), t.rfind('\n', 0, i), t.rfind(';', 0, i), t.rfind('!', 0, i)) + 1
    bs = [x for x in (t.find('.', j), t.find('\n', j), t.find(';', j), t.find('!', j)) if x != -1]
    return t[a:min(bs) if bs else len(t)].strip()


def negated(t, i, j):
    return bool(NEG_BEFORE.search(t[max(0, i - 60):i]) or NEG_AFTER.search(t[j:j + 60]))


def scan(text):
    """returns (strong hits, soft hits, long-only hits) as (label, sentence) lists"""
    strong, soft, long_ = [], [], []
    if not text:
        return strong, soft, long_
    for pat, label in STRONG:
        for m in re.finditer(pat, text, re.I):
            if not negated(text, m.start(), m.end()):
                strong.append((label, sentence(text, m.start(), m.end())))
    for rx in TERM:
        for m in rx.finditer(text):
            nums = [g for g in m.groups() if g and (g.isdigit() or g.lower() in NUM)]
            unit = re.search(r'(month|mo|night|day|week)', m.group(0), re.I)
            if not nums or not unit:
                continue
            if unit.group(1).lower() in ('night', 'day', 'week') or min(n_of(x) for x in nums) < 12:
                if not negated(text, m.start(), m.end()):
                    strong.append(('lease under 12 months', sentence(text, m.start(), m.end())))
    for pat, label in SOFT:
        for m in re.finditer(pat, text, re.I):
            # MLS feeds print "Furnished Description: Unfurnished"
            if re.match(r'\s*(description|desc\.?)?\s*:?\s*(unfurnished|no\b|none)', text[m.end():m.end() + 30], re.I):
                continue
            if not negated(text, m.start(), m.end()):
                soft.append((label, sentence(text, m.start(), m.end())))
    for m in LONG_ONLY.finditer(text):
        long_.append(('12+ months only', sentence(text, m.start(), m.end())))
    return strong, soft, long_


# Zillow resoFacts.leaseTerm / rentalCostsAndFees.leaseDuration
LT_LONG = re.compile(r'^\s*(1|one|12|18|24|2|two)[\s-]*(year|yr|month|mo)s?\b\s*(lease)?\s*$|^\s*(long[\s-]*term|annual|yearly)\s*$', re.I)
def zillow_lease(z):
    """(verdict or None, why) from Zillow's own lease fields"""
    if not z:
        return None, ''
    lt, ld = (z.get('lt') or '').strip(), z.get('ld')
    if lt:
        if re.search(r'month[\s-]*to[\s-]*month|short|vacation|seasonal|night|week|flex', lt, re.I):
            return 'yes', f'Zillow lease term: {lt}'
        nums = [n_of(x) for x in re.findall(N, lt, re.I)]
        if re.search(r'month', lt, re.I) and nums and min(nums) < 12:
            return 'yes', f'Zillow lease term: {lt}'
        if re.search(r'^\s*contact for details\s*$', lt, re.I):
            return None, ''  # Zillow's default when the landlord picked nothing
        if re.search(r'negotiable|varies|other|flex', lt, re.I):
            return 'maybe', f'Zillow lease term: {lt}'
        if LT_LONG.search(lt) or re.search(r'year|12', lt, re.I):
            return 'no', f'Zillow lease term: {lt}'
        return 'maybe', f'Zillow lease term: {lt}'
    if isinstance(ld, (int, float)) and ld > 0:
        return ('yes', f'Zillow lease length: {int(ld)} months') if ld < 12 else ('no', f'Zillow lease length: {int(ld)} months')
    return None, ''


def classify(h, texts, z=None):
    fj = (h.get('facts_josh') or {}).get('short_term')
    if fj is not None:
        return {'verdict': 'yes' if fj else 'no', 'why': 'You marked this yourself'}
    zv, zwhy = zillow_lease(z)
    text = '\n'.join(t for t in texts if t)
    strong, soft, long_ = scan(text)
    if z and z.get('fur') is True:
        soft.append(('furnished', 'Zillow lists it as furnished'))
    clip = lambda s: (s[:160] + '…') if len(s) > 160 else s
    if zv == 'yes':
        return {'verdict': 'yes', 'why': zwhy}
    if strong:
        lab, s = strong[0]
        return {'verdict': 'yes', 'why': f'Listing says {lab}: "{clip(s)}"' + (f' ({zwhy})' if zwhy else '')}
    if zv == 'maybe' or soft:
        if soft:
            lab, s = soft[0]
            why = f'Listing says {lab}: "{clip(s)}"' if not s.startswith('Zillow') else s
        else:
            why = zwhy
        if zwhy and zwhy not in why:
            why += f' ({zwhy})'
        return {'verdict': 'maybe', 'why': why}
    if zv == 'no':
        return {'verdict': 'no', 'why': zwhy}
    if long_:
        return {'verdict': 'no', 'why': f'Listing says: "{clip(long_[0][1])}"'}
    return {'verdict': 'unknown', 'why': 'No lease length in the listing text we have'}


def home_texts(h, extra):
    t = [h.get('listing_text'), h.get('notes'), h.get('specials'), h.get('pet_notes'), h.get('name'),
         ' '.join(h.get('amenities') or []) if isinstance(h.get('amenities'), list) else '']
    urls = [h.get('listing_url'), h.get('zillow_url'), h.get('redfin_url')] + list((h.get('links') or {}).values())
    for k in [h.get('id')] + [u.rstrip('/') for u in urls if isinstance(u, str) and u]:
        if k in extra:
            t.append(extra[k])
    return t


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    p = args[0]
    extra, zl = {}, {}
    for a in sys.argv[1:]:
        if a.startswith('--text='):
            for k, v in json.load(open(a[7:])).items():
                k = k.rstrip('/') if k.startswith('http') else k
                extra[k] = (extra.get(k, '') + '\n' + v) if k in extra else v
        if a.startswith('--zlease='):
            zl.update(json.load(open(a[9:])))
    d = json.load(open(p)); H = d if isinstance(d, list) else d['homes']
    now = datetime.datetime.now().strftime('%Y-%m-%d')
    from collections import Counter
    c = Counter()
    for h in H:
        z = zl.get(h['id'])
        texts = home_texts(h, extra) + ([z.get('desc'), z.get('other')] if z else [])
        r = classify(h, texts, z)
        r['at'] = now
        if '--write' in sys.argv:
            h['short_term'] = r
        if not h.get('hidden'):
            c[r['verdict']] += 1
    print('shown homes:', dict(c))
    if '--write' in sys.argv:
        json.dump(d, open(p, 'w'), separators=(',', ':'))


if __name__ == '__main__':
    main()
