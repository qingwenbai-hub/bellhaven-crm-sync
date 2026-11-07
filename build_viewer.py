"""Data quality checks on website + CRM data, and a local HTML viewer for manual comparison.

Reads  data/locations.json  (website scrape)
       data/crm_accounts.json (CRM pull)
Writes data/quality_issues.json
       viewer.html   (open in any browser, no server needed)
"""
import collections
import difflib
import json
import re

import match
import proposals

BELLHAVEN_PARENT = "0015QAPLGS3FVYEEEM"

norm_street = match.norm_street
norm_name = match.norm_name


def sim(a, b):
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 2)


def build(decisions=None, served=False, dry_run=False, write=True, quiet=False):
    """Build the viewer page. decisions = data/decisions.json contents (decided matches
    are shown as decided and never offered again). served=True when the review app
    serves the page, which enables the Accept / Decline buttons."""
    decisions = decisions or {}
    web = json.load(open("data/locations.json"))
    crm = json.load(open("data/crm_accounts.json"))
    by_id = {a["account_id"]: a for a in crm}
    parents = {a["account_id"]: a["name"] for a in crm if "(Parent Account)" in a["name"]}

    for a in crm:
        a["_is_parent"] = a["account_id"] in parents
        a["_street_n"] = norm_street(a["billing_street"])
        a["_name_n"] = norm_name(a["name"])
    for w in web:
        w["_street_n"] = norm_street(w["address"])
        w["_name_n"] = norm_name(w["name"])

    issues = []

    def add(kind, severity, ids, detail):
        issues.append({"kind": kind, "severity": severity, "ids": ids, "detail": detail})

    # ---------- CRM: missing values ----------
    for a in crm:
        if a["_is_parent"]:
            continue
        miss = [f for f in ["billing_street", "billing_city", "billing_state", "billing_zip", "care_type", "phone"]
                if not str(a[f]).strip()]
        if miss:
            add("CRM missing field", "medium", [a["account_id"]], f"{a['name']}: empty {', '.join(miss)}")
        if not a["parent_id"]:
            add("CRM facility with no parent", "low" if "bellhaven" not in a["name"].lower() else "high",
                [a["account_id"]], f"{a['name']} has no parent_id")
        if re.match(r"^\s*(po box|p\.o\.)", a["billing_street"], re.I):
            add("CRM PO Box as street", "medium", [a["account_id"]],
                f"{a['name']}: billing street is '{a['billing_street']}', not a physical address")
        if a["billing_zip"] and not re.fullmatch(r"\d{5}", a["billing_zip"]):
            add("CRM bad zip", "medium", [a["account_id"]], f"{a['name']}: zip '{a['billing_zip']}'")

    # ---------- CRM: duplicates ----------
    groups = collections.defaultdict(list)
    for a in crm:
        if not a["_is_parent"] and a["_street_n"]:
            groups[(a["_street_n"], a["billing_zip"])].append(a)
    for (st, z), g in groups.items():
        if len(g) > 1:
            add("CRM same address", "high", [x["account_id"] for x in g],
                f"{len(g)} accounts at {g[0]['billing_street']}, {z}: " +
                "; ".join(f"{x['name']} [{x['parent_name'] or 'no parent'}]" for x in g))
    groups = collections.defaultdict(list)
    for a in crm:
        groups[a["_name_n"]].append(a)
    for k, g in groups.items():
        if len(g) > 1:
            add("CRM same name", "high", [x["account_id"] for x in g],
                f"{len(g)} accounts named '{g[0]['name']}'")
    groups = collections.defaultdict(list)
    for a in crm:
        if a["phone"]:
            groups[a["phone"]].append(a)
    for k, g in groups.items():
        if len(g) > 1:
            add("CRM same phone", "medium", [x["account_id"] for x in g], f"phone {k}: " + ", ".join(x["name"] for x in g))

    # ---------- Website: missing / duplicates ----------
    for w in web:
        miss = [f for f in ["name", "address", "city", "state", "zip"] if not w[f]]
        if not w["care_offerings"]:
            miss.append("care_offerings")
        if miss:
            add("Website missing field", "medium", [w["slug"]], f"{w['name']}: empty {', '.join(miss)}")
    for key in ["_name_n", "_street_n", "phone"]:
        c = collections.defaultdict(list)
        for w in web:
            c[(w[key], w["zip"]) if key == "_street_n" else w[key]].append(w)
        for k, g in c.items():
            if len(g) > 1:
                add("Website duplicate", "high", [x["slug"] for x in g], f"{key}: " + ", ".join(x["name"] for x in g))

    # ---------- Matching (match.py) ----------
    m = match.run(web, crm)
    by_slug = {r["slug"]: r for r in m["locations"]}
    for w in web:
        r = by_slug[w["slug"]]
        w["candidates"] = r["candidates"]
        w["match"] = {k: r.get(k) for k in ["status", "reason", "matched_account_id", "fixes", "billing_flag", "chow_required", "possible", "same_address_others"]}
    for a in crm:
        a["billing_flag"] = match.billing_flag(a)

    # what Accept would write, for every candidate site (shown to the reviewer up front)
    plans = {}
    for w in web:
        for c in w["candidates"]:
            site = next(s for s in m["sites"] if s["site_id"] == c["site_id"])
            plans[f"{w['slug']}|{c['site_id']}"] = proposals.build_plan(w, site["account_ids"], c["account_id"], by_id)

    for w in web:
        if w["match"]["status"] != "confident":
            plans[f"{w['slug']}|NEW"] = proposals.build_create_plan(w)
    # accounts already handled by an accepted decision are not orphans
    handled = set()
    for d in decisions.values():
        if d.get("action") == "accept":
            handled.add(d.get("account_id"))
            for op in d.get("ops", []):
                handled.add(op.get("account_id"))
    site_of = m["site_of"]
    handled_sites = {site_of.get(a) for a in handled if a in site_of}
    orphans = [o for o in m["bellhaven_orphans"]
               if not o["is_candidate_elsewhere"] and o["account_id"] not in handled
               and site_of.get(o["account_id"]) not in handled_sites]
    for o in orphans:
        site = next(s for s in m["sites"] if s["site_id"] == site_of[o["account_id"]])
        plans[f"orphan|{o['account_id']}"] = proposals.build_orphan_plan(
            by_id[o["account_id"]], [by_id[x] for x in site["account_ids"]])

    for o in m["bellhaven_orphans"]:
        if not o["is_candidate_elsewhere"]:
            add("CRM under Bellhaven, not on website", "high", [o["account_id"]],
                f"{o['name']} ({o['city']}, {o['state']}) is under Bellhaven parent but matches no website location")
    for w in web:
        if w["match"]["status"] == "no_crm":
            add("Website location with no CRM account", "high", [w["slug"]],
                f"{w['name']} ({w['city']}, {w['state']}) has no CRM candidate at all")

    for a in crm:
        if a["billing_flag"]:
            add("Billing: revenue AND AR > 0", "medium", [a["account_id"]],
                f"{a['name']} [{a['parent_name'] or 'no parent'}]: revenue ${a['lifetime_revenue']:,}, AR ${a['outstanding_ar']:,}. Do not re-parent; use CHOW.")

    sev_rank = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: (sev_rank[i["severity"]], i["kind"]))
    json.dump(issues, open("data/quality_issues.json", "w"), indent=2)

    def strip(d):
        return {k: v for k, v in d.items() if not k.startswith("_")}

    payload = {
        "web": [strip(w) for w in web],
        "crm": [strip(a) for a in crm],
        "issues": issues,
        "bellhaven_parent": BELLHAVEN_PARENT,
        "orphans": orphans,
        "sites": m["sites"],
        "site_of": m["site_of"],
        "confident_score": match.CONFIDENT_SCORE,
        "abbr": match.ABBR,
        "weights": match.WEIGHTS,
        "abbr_phrases": match.ABBR_PHRASES,
        "plans": plans,
        "decisions": decisions,
        "served": served,
        "dry_run": dry_run,
    }
    html = open("viewer_template.html").read().replace("/*__DATA__*/null", json.dumps(payload))
    if write:
        open("viewer.html", "w").write(html)
    if not quiet:
        print(f"{len(web)} website rows, {len(crm)} CRM rows, {len(issues)} issues -> viewer.html")
    return html


def main():
    import os
    decisions = json.load(open("data/decisions.json")) if os.path.exists("data/decisions.json") else {}
    build(decisions)


if __name__ == "__main__":
    main()
