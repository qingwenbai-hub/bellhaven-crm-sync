"""Minimal CRM API client (Python standard library only, no installs needed)."""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://analyst-assessment-production.up.railway.app/api/v1"
HERE = os.path.dirname(os.path.abspath(__file__))


def token():
    t = os.environ.get("BH_TOKEN")
    env = os.path.join(HERE, ".env")
    if not t and os.path.exists(env):
        for line in open(env):
            if line.startswith("BH_TOKEN="):
                t = line.split("=", 1)[1].strip()
    if not t:
        raise SystemExit("No BH_TOKEN found (set it in .env or the environment)")
    return t


def _ssl_context():
    """python.org builds of Python on macOS ship without root certificates, which
    makes every HTTPS call fail with CERTIFICATE_VERIFY_FAILED. Fall back to
    certifi, or to the macOS system bundle, when the default store is empty."""
    ctx = ssl.create_default_context()
    if ctx.get_ca_certs() or ctx.cert_store_stats().get("x509_ca"):
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for path in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ctx


_CTX = _ssl_context()


class ApiError(Exception):
    pass


def _call(method, path, body=None, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
        "User-Agent": "bellhaven-crm-sync/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise ApiError(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')[:500]}")


def list_accounts():
    out, page = [], 1
    while True:
        d = _call("GET", "/accounts", params={"page": page, "page_size": 100})
        out += d["data"]
        if len(out) >= d["total"] or not d["data"]:
            return out
        page += 1


def get_account(account_id):
    return _call("GET", f"/accounts/{account_id}")


def patch_account(account_id, fields):
    return _call("PATCH", f"/accounts/{account_id}", body=fields)


def create_account(fields):
    return _call("POST", "/accounts", body=fields)
