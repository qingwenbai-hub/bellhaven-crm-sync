"""Match website locations to CRM accounts and classify each one.

Reads  data/locations.json, data/crm_accounts.json
Writes data/matches.json

Rules
-----
1. Collapse CRM accounts that share a normalized street + zip into one "site"
   (names, phones, parents become lists). Writes data/crm_sites.json.
2. Score each (website location, site) pair with weighted evidence.
  confident     exactly one site scores >= CONFIDENT_SCORE. The best account in
                that site is the match; the rest of the site are duplicates /
                old owner's records for review.
  needs_fix     everything else that has at least one candidate
  no_crm        no CRM account scored at all -> a new account is needed
Separately, CRM accounts under the Bellhaven parent that no website location
was confidently matched to are listed as orphans.

Billing flag (SOP): an account with lifetime_revenue > 0 AND outstanding_ar > 0
must not be re-parented; it needs the CHOW flow instead.
"""
import difflib
import json
import re

BELLHAVEN_PARENT = "0015QAPLGS3FVYEEEM"
CONFIDENT_SCORE = 80
MIN_CANDIDATE_SCORE = 30   # strict pass: below this an account is not shown as a candidate
WIDE_TOP_N = 8             # wide pass: how many "possible" accounts to show

# USPS-style abbreviations: every spelling of a word maps to one canonical token,
# so "West Main Street", "W. Main St" and "w main st" all compare equal.
_ABBR_GROUPS = {
    # directions
    "n": ["north", "no"], "s": ["south", "so"], "e": ["east"], "w": ["west"],
    "ne": ["northeast", "north east"], "nw": ["northwest", "north west"],
    "se": ["southeast", "south east"], "sw": ["southwest", "south west"],
    # street types
    "st": ["street", "str", "saint"], "ave": ["avenue", "av", "aven", "avn"],
    "rd": ["road"], "dr": ["drive", "drv", "driv"], "blvd": ["boulevard", "boul", "blv"],
    "ln": ["lane"], "ct": ["court", "crt"], "cir": ["circle", "circ", "circl", "crcl"],
    "pl": ["place"], "pkwy": ["parkway", "parkwy", "pky"], "hwy": ["highway", "highwy", "hiway"],
    "ter": ["terrace", "terr"], "trl": ["trail", "trails"], "sq": ["square", "sqr"],
    "tpke": ["turnpike", "trnpk"], "expy": ["expressway", "expw"], "fwy": ["freeway"],
    "pt": ["point"], "xing": ["crossing", "crssng"], "hts": ["heights", "ht"],
    "ctr": ["center", "centre", "cntr"], "rte": ["route"], "mt": ["mount", "mountain", "mtn"],
    "ft": ["fort", "frt"], "ste": ["suite"], "apt": ["apartment"], "bldg": ["building"],
    "rdg": ["ridge"], "crk": ["creek"], "hbr": ["harbor", "harbour"], "vly": ["valley"],
    "vlg": ["village", "vill"], "hl": ["hill"], "jct": ["junction"], "lk": ["lake"],
    "plz": ["plaza"], "aly": ["alley"], "byp": ["bypass"], "cv": ["cove"],
    "cres": ["crescent"], "est": ["estate", "estates"], "ext": ["extension"],
    "gln": ["glen"], "grv": ["grove"], "lndg": ["landing"], "mnr": ["manor"],
    "orch": ["orchard"], "spg": ["spring", "springs"], "sta": ["station"],
    "smt": ["summit"], "vw": ["view"], "way": ["wy"], "pike": ["pk"],
    # ordinals
    "1st": ["first"], "2nd": ["second"], "3rd": ["third"], "4th": ["fourth"], "5th": ["fifth"],
    "po box": ["post office box", "p o box", "pob"],
}
ABBR_PHRASES = sorted(((long, short) for short, longs in _ABBR_GROUPS.items() for long in longs if " " in long),
                      key=lambda p: -len(p[0]))
ABBR = {long: short for short, longs in _ABBR_GROUPS.items() for long in longs if " " not in long}


def norm_street(s):
    """Lowercase, strip punctuation, collapse every abbreviation to one canonical form."""
    s = " " + " ".join(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()) + " "
    for long, short in ABBR_PHRASES:
        s = s.replace(f" {long} ", f" {short} ")
    return " ".join(ABBR.get(t, t) for t in s.split())


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\brehab\b", "rehabilitation", s)
    s = re.sub(r"\bcentre\b", "center", s)
    s = re.sub(r"\bhealth care\b", "healthcare", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(the|at|of)\b", " ", s)
    return " ".join(ABBR.get(t, t) for t in s.split())


def sim(a, b):
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 2)


def is_parent_account(a):
    return "(Parent Account)" in a["name"]


def billing_flag(a):
    """True when SOP says: do not re-parent, use CHOW."""
    return (a.get("lifetime_revenue") or 0) > 0 and (a.get("outstanding_ar") or 0) > 0


# Weighted evidence. Total = 100.
# The street address identifies the physical building, so it carries the most weight.
# Phone is an independent confirmation (two records at the same address with
# different phones are probably a duplicate or an old owner's record).
# Name carries less weight because rebrands are exactly what we are trying to catch.
WEIGHTS = {"street": 40, "zip": 15, "phone": 15, "name": 15, "care": 10, "city": 5}

# website care offering -> CRM care_type
CARE_MAP = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Independent Living": "Independent Living",
}


# Words that say who owns the place, not which place it is. Ignored when comparing
# names so "Bellhaven of Batavia" is not "similar" to every other Bellhaven account.
BRAND_WORDS = {"bellhaven", "senior", "living", "care", "center", "ctr", "community", "communities"}


def name_core(s):
    return " ".join(t for t in norm_name(s).split() if t not in BRAND_WORDS)


def norm_phone(p):
    return re.sub(r"\D", "", p or "")[-10:]


# ---------------------------------------------------------------------------
# Step 1: collapse CRM accounts into sites
# ---------------------------------------------------------------------------
# Several CRM accounts often describe the same building: a duplicate entry, or the
# old owner's record left behind after an acquisition. They differ only in how the
# street is written ("1120 W Main St" vs "1120 West Main Street"). Grouping them by
# normalized street + zip first means each building is compared once, with every
# name / phone / parent it has ever had in the CRM.

def site_key(a):
    st = norm_street(a["billing_street"])
    if not st or not a["billing_zip"]:
        return None          # no usable address: the account stays on its own
    return f"{st}|{a['billing_zip']}"


def build_sites(facilities):
    groups = {}
    for a in facilities:
        key = site_key(a) or f"solo|{a['account_id']}"
        groups.setdefault(key, []).append(a)
    sites = []
    for key, accts in groups.items():
        accts = sorted(accts, key=lambda a: a["account_id"])
        first = accts[0]
        sites.append({
            "site_id": "S-" + first["account_id"],
            "key": key,
            "street": norm_street(first["billing_street"]),
            "zip": first["billing_zip"],
            "city": first["billing_city"],
            "state": first["billing_state"],
            "account_ids": [a["account_id"] for a in accts],
            "names": sorted({a["name"] for a in accts}),
            "phones": sorted({a["phone"] for a in accts if a["phone"]}),
            "parents": sorted({a["parent_name"] or "(no parent)" for a in accts}),
            "care_types": sorted({a["care_type"] for a in accts if a["care_type"]}),
            "street_spellings": sorted({a["billing_street"] for a in accts}),
        })
    sites.sort(key=lambda s: (-len(s["account_ids"]), s["key"]))
    return sites


# ---------------------------------------------------------------------------
# Step 2: score a website location against a site (or a single account)
# ---------------------------------------------------------------------------

def _as_site(a):
    return {"street": norm_street(a["billing_street"]), "zip": a["billing_zip"], "city": a["billing_city"],
            "names": [a["name"]], "phones": [a["phone"]] if a["phone"] else [],
            "care_types": [a["care_type"]] if a["care_type"] else []}


def score_site(w, site, wide=False):
    """Weighted score of one website location vs one site. A site can hold several
    names and phones; any matching phone counts and the best name counts.

    wide=False  strict pass: fuzzy street / name only count when clearly similar
    wide=True   second pass for locations with no match: every bit of similarity
                counts, so any site with some overlap shows up as a possibility
    """
    W = WEIGHTS
    score, why = 0.0, []
    ws, ss = norm_street(w["address"]), site["street"]
    if ss and ss == ws:
        score += W["street"]; why.append(f"street exact +{W['street']}")
    elif ss:
        r = sim(ss, ws)
        if r >= 0.8 or (wide and r >= 0.5):
            pts = W["street"] * r * 0.6
            score += pts; why.append(f"street ~{r} +{pts:.0f}")
    if site["zip"] and site["zip"] == w["zip"]:
        score += W["zip"]; why.append(f"zip +{W['zip']}")
    elif wide and site["zip"][:3] and site["zip"][:3] == w["zip"][:3]:
        pts = W["zip"] / 3
        score += pts; why.append(f"zip area +{pts:.0f}")
    if site["city"] and site["city"].lower() == w["city"].lower():
        score += W["city"]; why.append(f"city +{W['city']}")
    if norm_phone(w["phone"]) and norm_phone(w["phone"]) in {norm_phone(p) for p in site["phones"]}:
        score += W["phone"]; why.append(f"phone +{W['phone']}")
    # strict pass compares full names; wide pass drops brand words so only the
    # distinctive part ("batavia", "union square") is compared
    f = name_core if wide else norm_name
    ns = max((sim(f(n), f(w["name"])) for n in site["names"]), default=0)
    if ns >= 0.6 or (wide and ns >= 0.3):
        pts = W["name"] * ns
        score += pts; why.append(f"name ~{ns} +{pts:.0f}")
    web_care = {CARE_MAP.get(c, c) for c in w["care_offerings"]}
    if web_care & set(site["care_types"]):
        score += W["care"]; why.append(f"care +{W['care']}")
    return round(score), why


def score_pair(w, a, wide=False):
    return score_site(w, _as_site(a), wide)


def field_fixes(w, a):
    """What the CRM record would need to agree with the website."""
    fixes = []
    if a["parent_id"] != BELLHAVEN_PARENT:
        fixes.append({"field": "parent_id", "from": a["parent_name"] or "(none)", "to": "Bellhaven Senior Living"})
    if a["name"].strip() != w["name"].strip():
        fixes.append({"field": "name", "from": a["name"], "to": w["name"]})
    if norm_street(a["billing_street"]) != norm_street(w["address"]):
        fixes.append({"field": "billing_street", "from": a["billing_street"], "to": w["address"]})
    if a["billing_city"].strip().lower() != w["city"].strip().lower():
        fixes.append({"field": "billing_city", "from": a["billing_city"], "to": w["city"]})
    if a["billing_zip"] != w["zip"]:
        fixes.append({"field": "billing_zip", "from": a["billing_zip"], "to": w["zip"]})
    return fixes


def run(web, crm):
    facilities = [a for a in crm if not is_parent_account(a)]
    by_id = {a["account_id"]: a for a in crm}
    sites = build_sites(facilities)
    site_by_id = {s["site_id"]: s for s in sites}
    site_of = {aid: s["site_id"] for s in sites for aid in s["account_ids"]}

    def candidate(w, site, wide):
        sc, why = score_site(w, site, wide)
        # inside the site, rank accounts so the best one becomes the "primary"
        ranked = sorted(((score_pair(w, by_id[aid], wide)[0], aid) for aid in site["account_ids"]),
                        key=lambda t: (-t[0], by_id[t[1]]["parent_id"] != BELLHAVEN_PARENT, t[1]))
        primary = ranked[0][1]
        return {"site_id": site["site_id"], "account_id": primary, "score": sc, "why": why,
                "site_accounts": [{"account_id": aid, "score": s} for s, aid in ranked],
                "billing_flag": any(billing_flag(by_id[aid]) for aid in site["account_ids"])}

    results = []
    for w in web:
        cands = [c for c in (candidate(w, s, False) for s in sites) if c["score"] >= MIN_CANDIDATE_SCORE]
        cands.sort(key=lambda c: -c["score"])
        cands = cands[:4]

        # Wide pass: if the strict pass found no site at 80+, list every site with
        # any overlap (score > 0) so the reviewer can look for a rename / move.
        possible = []
        if not any(c["score"] >= CONFIDENT_SCORE for c in cands):
            shown = {c["site_id"] for c in cands}   # never repeat a site already listed above
            possible = [c for c in (candidate(w, s, True) for s in sites if s["site_id"] not in shown)
                        if c["score"] > 0]
            possible.sort(key=lambda c: -c["score"])
            possible = possible[:WIDE_TOP_N]

        top = cands[0] if cands else None
        high = [c for c in cands if c["score"] >= CONFIDENT_SCORE]
        if len(high) == 1:
            status, reason = "confident", f"only one site scored {CONFIDENT_SCORE}+ ({top['score']})"
        elif top:
            if top["score"] < CONFIDENT_SCORE:
                reason = f"best site only scored {top['score']} (needs {CONFIDENT_SCORE}+)"
            else:
                reason = f"{len(high)} different sites scored {CONFIDENT_SCORE}+"
            status = "needs_fix"
        else:
            status, reason = "no_crm", f"no CRM site scored {MIN_CANDIDATE_SCORE}+ (see possible matches)"

        entry = {"slug": w["slug"], "name": w["name"], "status": status, "reason": reason,
                 "candidates": cands, "possible": possible}
        if top:
            a = by_id[top["account_id"]]
            confident = status == "confident"
            entry["matched_site_id"] = top["site_id"] if confident else None
            entry["matched_account_id"] = a["account_id"] if confident else None
            entry["fixes"] = field_fixes(w, a) if confident else []
            # other accounts in the matched site: duplicates / old owner's records
            entry["same_address_others"] = [x["account_id"] for x in top["site_accounts"][1:]] if confident else []
            entry["billing_flag"] = billing_flag(a) if confident else any(c["billing_flag"] for c in cands)
            entry["chow_required"] = confident and a["parent_id"] != BELLHAVEN_PARENT and billing_flag(a)
        results.append(entry)

    matched_sites = {r["matched_site_id"] for r in results if r.get("matched_site_id")}
    candidate_sites = {c["site_id"] for r in results for c in r["candidates"] if c["score"] >= 50}
    orphans = []
    for a in facilities:
        if a["parent_id"] == BELLHAVEN_PARENT and site_of[a["account_id"]] not in matched_sites:
            orphans.append({
                "account_id": a["account_id"], "name": a["name"],
                "city": a["billing_city"], "state": a["billing_state"],
                "is_candidate_elsewhere": site_of[a["account_id"]] in candidate_sites,
                "billing_flag": billing_flag(a),
            })

    billing = [{"account_id": a["account_id"], "name": a["name"], "parent_name": a["parent_name"],
                "lifetime_revenue": a["lifetime_revenue"], "outstanding_ar": a["outstanding_ar"]}
               for a in facilities if billing_flag(a)]

    return {"locations": results, "sites": sites, "site_of": site_of,
            "bellhaven_orphans": orphans, "billing_flagged": billing,
            "settings": {"weights": WEIGHTS, "confident_score": CONFIDENT_SCORE,
                         "min_candidate_score": MIN_CANDIDATE_SCORE,
                         "bellhaven_parent": BELLHAVEN_PARENT}}


def main():
    web = json.load(open("data/locations.json"))
    crm = json.load(open("data/crm_accounts.json"))
    out = run(web, crm)
    json.dump(out, open("data/matches.json", "w"), indent=2)
    json.dump(out["sites"], open("data/crm_sites.json", "w"), indent=2)
    n_fac = len(out["site_of"])
    multi = [s for s in out["sites"] if len(s["account_ids"]) > 1]
    print(f"CRM facilities {n_fac} -> {len(out['sites'])} sites ({len(multi)} sites hold 2+ accounts)")
    locs = out["locations"]
    for st in ["confident", "needs_fix", "no_crm"]:
        print(f"{st:<10} {sum(1 for r in locs if r['status'] == st)}")
    print(f"orphans    {len(out['bellhaven_orphans'])}")
    print(f"billing    {len(out['billing_flagged'])} accounts with revenue AND AR > 0")


if __name__ == "__main__":
    main()
