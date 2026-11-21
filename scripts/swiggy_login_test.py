"""Swiggy MCP login test (CLAUDE.md §6A, J9) — READ-ONLY.
Runs on the laptop: registers a client (RFC 7591), opens Swiggy's own login page (you enter phone + OTP there),
catches the code on http://localhost:8765/callback, then calls ONLY read tools:
  /food : get_addresses, search_restaurants("veg lunch")
  /im   : get_addresses, search_products("soap")
Tokens are stored OUTSIDE the repo (%USERPROFILE%/.chinku/swiggy_tokens.json) and never printed.
Cart / checkout / payment / order tools are blocked in code — nothing here can spend money.
Run: python -m scripts.swiggy_login_test"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

BASE = "https://mcp.swiggy.com"
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/callback"
HOME = Path(os.environ.get("USERPROFILE") or Path.home()) / ".chinku"
CLIENT = HOME / "swiggy_client.json"
# --account <name>: log in a SECOND Swiggy account (its own phone + OTP). Default = the first account ("main").
ACCOUNT = sys.argv[sys.argv.index("--account") + 1].lower() if "--account" in sys.argv else "main"
assert re.match(r"^[a-z0-9_-]{1,24}$", ACCOUNT), "account name: letters, digits, - or _ (max 24)"
TOKENS = HOME / "swiggy_tokens.json" if ACCOUNT == "main" else HOME / "swiggy" / ACCOUNT / "tokens.json"
FRESH = "--account" in sys.argv  # a named account always logs in fresh (never reuses another account's tokens)
READ_ONLY = {"get_addresses", "search_restaurants", "search_menu", "get_restaurant_menu", "search_products", "your_go_to_items"}


def save(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def register() -> dict:
    if CLIENT.exists():
        return json.loads(CLIENT.read_text(encoding="utf-8"))
    r = httpx.post(f"{BASE}/auth/register", json={
        "client_name": "Chinku (hackathon prototype, read-only test)", "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": "mcp:tools"}, timeout=30)
    print("register:", r.status_code)
    if r.status_code >= 400:
        sys.exit(f"Client registration refused: {r.text[:400]}")
    c = r.json()
    save(CLIENT, c)
    return c


def login(client: dict) -> dict:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Chinku: Swiggy login received. You can close this tab.</h2>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"{BASE}/auth/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT, "scope": "mcp:tools",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    print(f"\n>>> Logging in Swiggy account “{ACCOUNT}”. Enter THAT account's phone number and OTP on Swiggy's page.")
    if ACCOUNT != "main":
        print(">>> Your browser may still be logged in as your first number — open this link in a PRIVATE / INCOGNITO window:")
        print(url, "\n", flush=True)
    else:
        print(">>> If no tab opens, paste this into your browser:\n", url, "\n", flush=True)
        webbrowser.open(url)
    t0 = time.time()
    while "code" not in got and "error" not in got and time.time() - t0 < 300:
        time.sleep(0.5)
    srv.shutdown()
    if "error" in got or "code" not in got:
        sys.exit(f"Login not completed: {got.get('error')} {got.get('error_description', '')}")
    if got.get("state") != state:
        sys.exit("State mismatch — login aborted for safety.")
    r = httpx.post(f"{BASE}/auth/token", json={"grant_type": "authorization_code", "code": got["code"], "code_verifier": verifier,
                                               "redirect_uri": REDIRECT, "client_id": client["client_id"]}, timeout=30)
    print("token:", r.status_code)
    if r.status_code >= 400:
        sys.exit(f"Token exchange failed: {r.text[:300]}")
    tok = r.json() | {"obtained_at": time.time()}
    save(TOKENS, tok)
    return tok


def tokens(client: dict) -> dict:
    if TOKENS.exists() and not FRESH:
        t = json.loads(TOKENS.read_text(encoding="utf-8"))
        if time.time() - t["obtained_at"] < t.get("expires_in", 0) - 300:
            return t
        r = httpx.post(f"{BASE}/auth/token", json={"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                                   "client_id": client["client_id"]}, timeout=30)
        if r.status_code < 400:
            t = r.json() | {"obtained_at": time.time()}
            save(TOKENS, t)
            return t
    return login(client)


class MCP:
    def __init__(self, server: str, access: str):
        self.url, self.sid, self.n = f"{BASE}/{server}", None, 0
        self.h = {"Authorization": f"Bearer {access}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def _post(self, msg: dict) -> dict | None:
        h = dict(self.h) | ({"Mcp-Session-Id": self.sid} if self.sid else {})
        r = httpx.post(self.url, json=msg, headers=h, timeout=60)
        self.sid = r.headers.get("mcp-session-id") or self.sid
        if r.status_code >= 400:
            raise RuntimeError(f"{self.url} HTTP {r.status_code}: {r.text[:300]}")
        if "id" not in msg:
            return None
        body = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            datas = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
            body = next((d for d in reversed(datas) if '"id"' in d), datas[-1] if datas else "{}")
        return json.loads(body)

    def req(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        out = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        if out.get("error"):
            raise RuntimeError(f"{method}: {out['error']}")
        return out.get("result", {})

    def start(self) -> list[dict]:
        self.req("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "chinku", "version": "0.1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.req("tools/list").get("tools", [])

    def call(self, name: str, args: dict) -> dict:
        if name not in READ_ONLY:
            raise PermissionError(f"{name} is blocked — read-only test")
        res = self.req("tools/call", {"name": name, "arguments": args})
        txt = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        try:
            return res.get("structuredContent") or json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            return {"text": txt}


def schema(tools: list[dict], name: str) -> dict:
    t = next((t for t in tools if t["name"] == name), None)
    return (t or {}).get("inputSchema", {}).get("properties", {}) if t else {}


def pick(props: dict, words: tuple) -> str | None:
    return next((k for k in props if any(w in k.lower() for w in words)), None)


def first_address(data) -> tuple[str | None, str]:
    s = json.dumps(data)
    for key in ("addressId", "address_id", "id"):
        rows = data if isinstance(data, list) else next((v for v in (data or {}).values() if isinstance(v, list)), [])
        for a in rows if isinstance(rows, list) else []:
            if isinstance(a, dict) and a.get(key):
                return str(a[key]), str(a.get("annotation") or a.get("label") or a.get("tag") or "address")
    return None, s[:80]


def main() -> None:
    client = register()
    print("client registered:", "yes (id kept private)")
    tok = tokens(client)
    if FRESH:  # a second account: just log in (no test searches — be gentle with Swiggy)
        print(f"\nLogged in Swiggy account “{ACCOUNT}”. Tokens saved outside the repo: {TOKENS}")
        print(f"Next: python -m scripts.swiggy_upload {ACCOUNT}   (puts it on the server; then pick it in Settings)")
        return
    report = {}
    for server, query, search_tool in (("food", "veg lunch", "search_restaurants"), ("im", "soap", "search_products")):
        m = MCP(server, tok["access_token"])
        tools = m.start()
        print(f"\n== /{server}: {len(tools)} tools →", ", ".join(sorted(t["name"] for t in tools)))
        addrs = m.call("get_addresses", {})
        aid, label = first_address(addrs)
        print(f"addresses: first = {label} (id kept private)" if aid else f"addresses: none found ({label})")
        props = schema(tools, search_tool)
        args = {}
        qk = pick(props, ("query", "search", "keyword", "term"))
        ak = pick(props, ("address",))
        if qk:
            args[qk] = query
        if ak and aid:
            args[ak] = aid
        print(f"{search_tool} inputs: {list(props)} → calling with {[k for k in args]}")
        res = m.call(search_tool, args)
        report[server] = res
        print(json.dumps(res, ensure_ascii=False)[:1800])
    out = HOME / "swiggy_test_results.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nFull results saved (outside the repo): {out}")


if __name__ == "__main__":
    main()
