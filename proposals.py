"""Turn an accepted match into the exact CRM writes, following the team's rules.

A match = (website location W, CRM site S, primary account P in S).
Accepting it means "W is the truth for this building":

1. Primary account P gets W's name, street, city, state, zip, phone (only fields
   that actually differ are sent) and status Active. care_type is only changed
   when P's value is not one of W's care offerings.
2. If P is not under the Bellhaven parent:
     - P has lifetime_revenue > 0 AND outstanding_ar > 0  (billing SOP / CHOW):
         P is left exactly as it is, a NEW account is created for the facility
         under Bellhaven with W's data, and P.chow_current_account = new id.
     - otherwise: P is re-parented to Bellhaven directly (with the field fixes).
3. Every other account in S (same building, old owner's record or duplicate)
   gets duplicate_of_account = the surviving account id and status Inactive.
   There is no merge or delete in the API, so this is how a duplicate is retired.

Nothing here talks to the API. It only builds a list of operations, which the
review app shows to the reviewer and executes only after "Accept".
"""
import datetime

from match import BELLHAVEN_PARENT, CARE_MAP, billing_flag, norm_phone, norm_street

TAG = "bellhaven-sync"


def _today():
    return datetime.date.today().isoformat()


def _note(existing, text):
    line = f"[{TAG} {_today()}] {text}"
    return f"{existing}\n{line}" if (existing or "").strip() else line


def target_fields(w, a):
    """Website values for the CRM fields we keep in sync, only where they differ."""
    want = {
        "name": w["name"],
        "billing_street": w["address"],
        "billing_city": w["city"],
        "billing_state": w["state"],
        "billing_zip": w["zip"],
        "phone": w["phone"],
    }
    out = {}
    for k, v in want.items():
        cur = a.get(k) or ""
        same = (norm_street(cur) == norm_street(v) and cur.strip().lower() == v.strip().lower()) \
            if k == "billing_street" else \
            (norm_phone(cur) == norm_phone(v)) if k == "phone" else \
            (cur.strip() == v.strip())
        if not same and v:
            out[k] = v
    web_care = [CARE_MAP.get(c, c) for c in w["care_offerings"]]
    if web_care and a.get("care_type") not in web_care:
        out["care_type"] = web_care[0]
    if a.get("status") != "Active":
        out["status"] = "Active"
    return out


def build_plan(w, site_account_ids, primary_id, by_id):
    """Return the ordered list of operations for accepting this match.

    Each op is a dict:
      {"op": "patch",  "account_id": ..., "fields": {...}, "before": {...}, "why": "..."}
      {"op": "create", "ref": "NEW", "fields": {...}, "why": "..."}
    A value "@NEW" in fields means "the id of the account created by the create op".
    """
    p = by_id[primary_id]
    ops = []
    reparent = p["parent_id"] != BELLHAVEN_PARENT
    chow = reparent and billing_flag(p)

    if chow:
        fields = {
            "name": w["name"], "parent_id": BELLHAVEN_PARENT,
            "billing_street": w["address"], "billing_city": w["city"],
            "billing_state": w["state"], "billing_zip": w["zip"], "phone": w["phone"],
            "care_type": ([CARE_MAP.get(c, c) for c in w["care_offerings"]] or [p.get("care_type")])[0],
            "status": "Active",
            "note": _note("", f"Created by CHOW from {p['account_id']} ({p['name']}, "
                              f"was under {p['parent_name']}). Source: Bellhaven website {w['url']}"),
        }
        ops.append({"op": "create", "ref": "NEW", "fields": fields,
                    "why": f"CHOW: {p['name']} has revenue ${p['lifetime_revenue']:,} and AR "
                           f"${p['outstanding_ar']:,}, so the old account is preserved and a new one "
                           f"is created under Bellhaven"})
        ops.append({"op": "patch", "account_id": p["account_id"],
                    "fields": {"chow_current_account": "@NEW"},
                    "before": {"chow_current_account": p.get("chow_current_account", "")},
                    "why": "CHOW: point the old account at the new one; nothing else on it changes"})
        survivor = "@NEW"
    elif billing_flag(p):
        # Billing SOP: an account with revenue AND outstanding AR is never edited.
        # It already sits under Bellhaven, so there is no CHOW to do either.
        survivor = p["account_id"]
    else:
        fields = target_fields(w, p)
        if reparent:
            fields["parent_id"] = BELLHAVEN_PARENT
        if fields:
            what = ", ".join(sorted(fields))
            fields["note"] = _note(p.get("note"), f"Updated from Bellhaven website ({what}). {w['url']}")
            ops.append({"op": "patch", "account_id": p["account_id"], "fields": fields,
                        "before": {k: p.get(k, "") for k in fields},
                        "why": ("Re-parent to Bellhaven (no revenue + AR, so direct re-parent is allowed)"
                                if reparent else "Bring CRM fields in line with the website")})
        survivor = p["account_id"]

    for aid in site_account_ids:
        if aid == primary_id:
            continue
        o = by_id[aid]
        if billing_flag(o):
            # old owner's record with billing history at the same building: treat as a
            # change of ownership. Only chow_current_account is set; nothing else changes.
            if o.get("chow_current_account"):
                continue
            ops.append({"op": "patch", "account_id": aid,
                        "fields": {"chow_current_account": survivor},
                        "before": {"chow_current_account": o.get("chow_current_account", "")},
                        "why": f"CHOW: old owner's record at the same address with revenue "
                               f"${o['lifetime_revenue']:,} and AR ${o['outstanding_ar']:,}. "
                               f"Only chow_current_account is set; nothing else on it changes"})
            continue
        if o.get("duplicate_of_account") and o.get("status") == "Inactive":
            continue
        ops.append({"op": "patch", "account_id": aid,
                    "fields": {"duplicate_of_account": survivor, "status": "Inactive",
                               "note": _note(o.get("note"),
                                             f"Duplicate: same building as {w['name']} ({w['address']}, "
                                             f"{w['city']}). Surviving account: {survivor if survivor != '@NEW' else 'new CHOW account'}"
                                             + (f". Note: this record has revenue ${o['lifetime_revenue']:,} / AR ${o['outstanding_ar']:,}"
                                                if billing_flag(o) else ""))},
                    "before": {"duplicate_of_account": o.get("duplicate_of_account", ""),
                               "status": o.get("status", "")},
                    "why": f"Same address as the matched account; under {o['parent_name'] or 'no parent'}. "
                           f"Retire as a duplicate (Inactive)."})
    return ops


def build_create_plan(w):
    """Website location with no usable CRM account: create one under Bellhaven."""
    fields = {
        "name": w["name"], "parent_id": BELLHAVEN_PARENT,
        "billing_street": w["address"], "billing_city": w["city"],
        "billing_state": w["state"], "billing_zip": w["zip"], "phone": w["phone"],
        "care_type": ([CARE_MAP.get(c, c) for c in w["care_offerings"]] or [""])[0],
        "status": "Active",
        "note": _note("", f"Created from Bellhaven website listing {w['url']}"),
    }
    return [{"op": "create", "ref": "NEW", "fields": fields,
             "why": "No CRM account matches this website location, so a new account is created under Bellhaven"}]


def build_orphan_plan(a, same_address=None):
    """CRM account under the Bellhaven parent that is no longer on the website.

    If another active account at the same address sits under a different parent,
    that operator most likely bought the facility:
      - revenue AND AR (billing SOP): leave the old account exactly as it is and set
        chow_current_account to the new owner's existing account
      - otherwise: retire the old account as a duplicate of the new owner's account
    If there is no such account, the new owner is unknown, so the parent is NOT
    changed; the account is flagged for a human to research."""
    buyers = [o for o in (same_address or [])
              if o["account_id"] != a["account_id"] and o.get("parent_id")
              and o["parent_id"] != BELLHAVEN_PARENT and o.get("status") != "Inactive"]
    if buyers:
        b = buyers[0]
        owner = (b.get("parent_name") or "").replace(" (Parent Account)", "")
        if billing_flag(a):
            return [{"op": "patch", "account_id": a["account_id"],
                     "fields": {"chow_current_account": b["account_id"]},
                     "before": {"chow_current_account": a.get("chow_current_account", "")},
                     "why": f"CHOW: {b['name']} ({owner}) is at the same address, so the facility was sold to "
                            f"{owner}. Revenue ${a['lifetime_revenue']:,} and AR ${a['outstanding_ar']:,}: the old "
                            f"account is not edited; only chow_current_account points to the {owner} account"}]
        return [{"op": "patch", "account_id": a["account_id"],
                 "fields": {"duplicate_of_account": b["account_id"], "status": "Inactive",
                            "note": _note(a.get("note"), f"Not on the Bellhaven website; {b['name']} ({owner}) "
                                                         f"is at the same address. Retired as a duplicate of {b['account_id']}.")},
                 "before": {"duplicate_of_account": a.get("duplicate_of_account", ""), "status": a.get("status", "")},
                 "why": f"Sold to {owner}: their account at the same address survives"}]
    text = (f"Under Bellhaven in CRM but not listed on the Bellhaven website (checked {_today()}). "
            f"Possible sale, closure or rebrand. Parent left unchanged because the new owner is unknown.")
    if billing_flag(a):
        text += (f" Billing history: revenue ${a['lifetime_revenue']:,}, AR ${a['outstanding_ar']:,}; "
                 f"if it was sold, follow the CHOW SOP (do not re-parent this account).")
    return [{"op": "patch", "account_id": a["account_id"],
             "fields": {"status": "Needs Review", "note": _note(a.get("note"), text)},
             "before": {"status": a.get("status", "")},
             "why": "Not on the website any more; flag for research instead of guessing a new parent"}]
