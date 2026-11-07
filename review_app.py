"""Local review app. Run:  python3 review_app.py   then open http://localhost:8765

The page shows every proposed match with its evidence. Nothing is written to the
CRM until the reviewer clicks Accept; Decline only records the decision.
Every decision is saved in data/decisions.json, so re-running the pipeline never
proposes an already decided match again.

Set DRY_RUN=1 to click through without writing to the CRM (writes are logged
to data/decisions_dryrun.json instead).
"""
import datetime
import json
import os
import sys
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import build_viewer
import crm_api
import match
import proposals

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
DRY_RUN = os.environ.get("DRY_RUN") == "1"
DECISIONS = "data/decisions_dryrun.json" if DRY_RUN else "data/decisions.json"
PORT = int(os.environ.get("PORT", "8765"))


def load_decisions():
    return json.load(open(DECISIONS)) if os.path.exists(DECISIONS) else {}


def save_decisions(d):
    tmp = DECISIONS + ".tmp"
    json.dump(d, open(tmp, "w"), indent=2)
    os.replace(tmp, DECISIONS)


def decision_key(slug, account_id):
    # keyed by location + primary CRM account, which stays stable across re-runs
    # (site ids can change when a CHOW creates a new account at the same address)
    return f"{slug}|{account_id}"


def refresh_crm():
    rows = crm_api.list_accounts()
    json.dump(rows, open("data/crm_accounts.json", "w"), indent=2)
    return len(rows)


def run_ops(ops):
    done, new_id = [], None
    for op in ops:
        fields = {k: (new_id if v == "@NEW" else v) for k, v in op["fields"].items()}
        if op["op"] == "create":
            if DRY_RUN:
                res = {"account_id": "DRYRUN-" + uuid.uuid4().hex[:8]}
            else:
                res = crm_api.create_account(fields)
                res = res.get("data", res) if isinstance(res, dict) else res
            new_id = res["account_id"]
            done.append({"op": "create", "account_id": new_id, "fields": fields, "why": op["why"]})
        else:
            if not DRY_RUN:
                crm_api.patch_account(op["account_id"], fields)
            done.append({"op": "patch", "account_id": op["account_id"], "fields": fields,
                         "before": op.get("before", {}), "why": op["why"]})
    return done


def accept_create(slug):
    web = {w["slug"]: w for w in json.load(open("data/locations.json"))}[slug]
    return run_ops(proposals.build_create_plan(web))


def accept_orphan(account_id):
    m = json.load(open("data/matches.json"))
    site = next(s for s in m["sites"] if account_id in s["account_ids"])
    accts = [crm_api.get_account(x) for x in site["account_ids"]]
    me = next(x for x in accts if x["account_id"] == account_id)
    return run_ops(proposals.build_orphan_plan(me, accts))


def accept(slug, site_id, account_id):
    web = {w["slug"]: w for w in json.load(open("data/locations.json"))}[slug]
    m = json.load(open("data/matches.json"))
    site = {s["site_id"]: s for s in m["sites"]}[site_id]
    if account_id not in site["account_ids"]:
        raise ValueError("account is not part of this site")
    # always plan against fresh CRM data, never a stale snapshot
    by_id = {aid: crm_api.get_account(aid) for aid in site["account_ids"]}
    ops = proposals.build_plan(web, site["account_ids"], account_id, by_id)

    done, new_id = [], None
    for op in ops:
        fields = {k: (new_id if v == "@NEW" else v) for k, v in op["fields"].items()}
        if op["op"] == "create":
            if DRY_RUN:
                res = {"account_id": "DRYRUN-" + uuid.uuid4().hex[:8]}
            else:
                res = crm_api.create_account(fields)
                res = res.get("data", res) if isinstance(res, dict) else res
            new_id = res["account_id"]
            done.append({"op": "create", "account_id": new_id, "fields": fields, "why": op["why"]})
        else:
            if not DRY_RUN:
                crm_api.patch_account(op["account_id"], fields)
            done.append({"op": "patch", "account_id": op["account_id"], "fields": fields,
                         "before": op["before"], "why": op["why"]})
    return done


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # allow the static viewer.html (opened from disk) to talk to this local server
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + fmt % args + "\n")

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = build_viewer.build(decisions=load_decisions(), served=True, dry_run=DRY_RUN, write=False)
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/ping":
            self._send(200, json.dumps({"ok": True, "dry_run": DRY_RUN}))
        elif self.path == "/api/decisions":
            self._send(200, json.dumps(load_decisions()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/decide":
                kind = body.get("kind", "match")
                slug, site_id, action = body.get("slug", ""), body.get("site_id", ""), body["action"]
                decisions = load_decisions()
                if kind != "orphan" and action == "accept" and any(
                        d["slug"] == slug and d["action"] == "accept" for d in decisions.values()):
                    return self._send(409, json.dumps({"error": "this location already has an accepted match"}))
                if kind == "create":
                    key = decision_key(slug, "NEW")
                elif kind == "orphan":
                    key = decision_key("orphan", body["account_id"])
                else:
                    key = decision_key(slug, body.get("account_id"))
                if key in decisions:
                    return self._send(409, json.dumps({"error": "already decided", "decision": decisions[key]}))
                if action not in ("accept", "decline"):
                    return self._send(400, json.dumps({"error": "action must be accept or decline"}))
                rec = {"kind": kind, "slug": slug if kind != "orphan" else "orphan", "site_id": site_id, "account_id": body.get("account_id"),
                       "action": action, "reason": body.get("reason", ""),
                       "at": datetime.datetime.now().isoformat(timespec="seconds"), "dry_run": DRY_RUN}
                if action == "accept":
                    try:
                        if kind == "create":
                            rec["ops"] = accept_create(slug)
                        elif kind == "orphan":
                            rec["ops"] = accept_orphan(body["account_id"])
                        else:
                            rec["ops"] = accept(slug, site_id, body["account_id"])
                    except Exception as e:
                        traceback.print_exc()
                        # nothing is recorded, so the reviewer can retry
                        return self._send(502, json.dumps({"error": str(e)}))
                decisions[key] = rec
                save_decisions(decisions)
                self._send(200, json.dumps({"ok": True, "decision": rec}))
            elif self.path == "/api/refresh":
                n = refresh_crm()
                match.main()
                self._send(200, json.dumps({"ok": True, "accounts": n}))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            traceback.print_exc()
            self._send(500, json.dumps({"error": str(e)}))


def main():
    if not os.path.exists("data/matches.json"):
        match.main()
    try:
        crm_api._call("GET", "/me")
    except Exception as e:
        print(f"WARNING: could not reach the CRM API ({e}).")
        print("Check BH_TOKEN in .env. If the error mentions SSL certificates on macOS, run")
        print("'/Applications/Python 3.x/Install Certificates.command' once.")
    url = f"http://localhost:{PORT}"
    print(f"Review app running on {url}" + ("   [DRY RUN: no CRM writes]" if DRY_RUN else ""))
    print("Keep this window open while reviewing. Press Ctrl+C to stop.")
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    if os.environ.get("NO_BROWSER") != "1":
        import threading, webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
