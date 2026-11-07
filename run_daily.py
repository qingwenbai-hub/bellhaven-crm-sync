"""Daily pipeline: scrape the website, pull the CRM, match, and queue proposals.

Steps
  1. scraper.py      -> data/locations.json     (every Bellhaven community)
  2. crm_api         -> data/crm_accounts.json  (every CRM account)
  3. match.py        -> data/matches.json       (scores + classification)
  4. build_viewer.py -> viewer.html             (review page with evidence)
  5. data/pending_proposals.json: proposals that still need a human decision

Safe to re-run: every Accept / Decline is stored in data/decisions.json and a
decided proposal is never queued again. Nothing is written to the CRM here;
writes only happen when a reviewer clicks Accept in the review app.
"""
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

import build_viewer  # noqa: E402
import crm_api  # noqa: E402
import match  # noqa: E402


def main():
    skip_scrape = "--skip-scrape" in sys.argv
    if not skip_scrape:
        subprocess.run([sys.executable, "scraper.py"], check=True)
    rows = crm_api.list_accounts()
    json.dump(rows, open("data/crm_accounts.json", "w"), indent=2)
    print(f"Fetched {len(rows)} CRM accounts")
    match.main()

    decisions = json.load(open("data/decisions.json")) if os.path.exists("data/decisions.json") else {}
    build_viewer.build(decisions)

    m = json.load(open("data/matches.json"))
    accepted_slugs = {d["slug"] for d in decisions.values() if d["action"] == "accept"}
    decided_keys = set(decisions)
    pending = []
    for r in m["locations"]:
        if r["slug"] in accepted_slugs:
            continue
        for c in r["candidates"]:
            ids = [x["account_id"] for x in c["site_accounts"]]
            if any(f"{r['slug']}|{a}" in decided_keys for a in ids):
                continue
            pending.append({"type": "match", "location": r["name"], "status": r["status"],
                            "account_id": c["account_id"], "score": c["score"]})
        if r["status"] != "confident" and f"{r['slug']}|NEW" not in decided_keys:
            pending.append({"type": "create", "location": r["name"]})
    for o in m["bellhaven_orphans"]:
        if not o["is_candidate_elsewhere"] and f"orphan|{o['account_id']}" not in decided_keys:
            pending.append({"type": "orphan", "account": o["name"], "account_id": o["account_id"]})

    out = {"run_at": datetime.datetime.now().isoformat(timespec="seconds"), "pending": pending}
    json.dump(out, open("data/pending_proposals.json", "w"), indent=2)
    print(f"{len(pending)} proposals waiting for review ({len(decisions)} already decided)")


if __name__ == "__main__":
    main()
