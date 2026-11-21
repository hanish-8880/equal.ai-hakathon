"""Swiggy Instamart — LIVE adapter over Swiggy's official MCP (mcp.swiggy.com/im), the user's own login (OAuth 2.1 + PKCE,
done once with scripts/swiggy_login_test.py; tokens kept OUTSIDE the repo in ~/.chinku/, refreshed + rotated here).
Observed (DECISIONS Sun 01:30): search_products(query, addressId) → products[].variations[{spinId, skuId,
quantityDescription, price{mrp, offerPrice, unitLevelPrice}, isInStockAndAvailable}]; update_cart REPLACES the whole cart.
Safety in code: only ALLOWED tools can be called. Chinku never checks out, never clears the cart, never touches payment —
adding to the cart merges with what is already there and only happens after the user's yes."""
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

BASE = "https://mcp.swiggy.com"
ALLOWED = {  # per server. NEVER: checkout, place_food_order, confirm_order, payment tools, clear/flush cart
    "im": {"get_addresses", "search_products", "get_cart", "update_cart", "your_go_to_items"},
    "food": {"get_addresses", "search_restaurants", "search_menu", "get_restaurant_menu", "get_food_cart", "update_food_cart",
             "fetch_food_coupons"},
    "dineout": set(),  # filled after the tool list is verified (scripts/swiggy_tools.py)
}
_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}


class SwiggyError(Exception):
    pass


def home() -> Path:
    return Path(os.environ.get("CHINKU_SECRETS_DIR") or (Path(os.environ.get("USERPROFILE") or Path.home()) / ".chinku"))


def connected() -> bool:
    return (home() / "swiggy_tokens.json").exists()


def _token() -> str:
    tf, cf = home() / "swiggy_tokens.json", home() / "swiggy_client.json"
    if not tf.exists():
        raise SwiggyError("Swiggy is not connected yet (log in once with your phone + OTP).")
    t = json.loads(tf.read_text(encoding="utf-8"))
    if time.time() - t.get("obtained_at", 0) < t.get("expires_in", 0) - 600:
        return t["access_token"]
    client = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {}
    r = httpx.post(f"{BASE}/auth/token", json={"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                               "client_id": client.get("client_id", "swiggy-mcp")}, timeout=30)
    if r.status_code >= 400:
        raise SwiggyError("Swiggy login expired — please log in again.")
    t = r.json() | {"obtained_at": time.time()}
    tf.write_text(json.dumps(t), encoding="utf-8")  # refresh tokens rotate: always keep the newest
    return t["access_token"]


def _post(msg: dict[str, Any], tok: str, sid: str | None, server: str = "im") -> tuple[dict[str, Any] | None, str | None]:
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if sid:
        h["Mcp-Session-Id"] = sid
    r = httpx.post(f"{BASE}/{server}", json=msg, headers=h, timeout=60)
    sid = r.headers.get("mcp-session-id") or sid
    if r.status_code == 401:
        raise SwiggyError("Swiggy login expired — please log in again.")
    if r.status_code >= 400:
        raise SwiggyError(f"Swiggy is busy right now (HTTP {r.status_code}).")
    if "id" not in msg:
        return None, sid
    body = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        datas = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        body = next((d for d in reversed(datas) if '"id"' in d), datas[-1] if datas else "{}")
    return json.loads(body or "{}"), sid


def call(name: str, args: dict[str, Any], server: str = "im") -> dict[str, Any]:
    if name not in ALLOWED.get(server, set()):
        raise PermissionError(f"Chinku never calls Swiggy's “{name}” (checkout, payment and clearing the cart are yours to do).")
    with _lock:  # one session per user per server, calls in sequence (Swiggy's guidance)
        tok = _token()
        ses = _sessions.setdefault(server, {})
        if ses.get("tok") != tok or time.time() - ses.get("at", 0) > 600:
            out, sid = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "chinku", "version": "0.1"}}},
                             tok, None, server)
            _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, tok, sid, server)
            ses.update(tok=tok, sid=sid, at=time.time(), n=1)
        ses["n"] += 1
        out, _ = _post({"jsonrpc": "2.0", "id": ses["n"], "method": "tools/call", "params": {"name": name, "arguments": args}},
                       tok, ses.get("sid"), server)
    if (out or {}).get("error"):
        raise SwiggyError(f"Swiggy: {out['error'].get('message', 'error')}")
    res = (out or {}).get("result") or {}
    txt = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    if res.get("isError"):
        raise SwiggyError(f"Swiggy: {txt[:200]}")
    if res.get("structuredContent"):
        return res["structuredContent"]
    try:
        return json.loads(txt)
    except (json.JSONDecodeError, TypeError):
        return {"text": txt}


def address_id(server: str = "im") -> str:
    ses = _sessions.setdefault(server, {})
    if ses.get("addr"):
        return ses["addr"]
    data = call("get_addresses", {}, server)
    rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
    a = next((r for r in rows if isinstance(r, dict) and (r.get("id") or r.get("addressId"))), None)
    if not a:
        raise SwiggyError("No delivery address on your Swiggy account — add one in the Swiggy app first.")
    ses["addr"] = str(a.get("id") or a.get("addressId"))
    ses["addr_label"] = str(a.get("annotation") or a.get("label") or a.get("tag") or "your address")
    return ses["addr"]


def grams(desc: str | None) -> float | None:
    m = re.match(r"\s*([\d.]+)\s*(kg|g|gm|l|ltr|ml)\b", (desc or "").lower())
    if not m:
        return None
    v = float(m.group(1))
    return v * 1000 if m.group(2) in ("kg", "l", "ltr") else v


def search(query: str) -> list[dict[str, Any]]:
    """Flattened, in-stock variants for one query (code picks; the model never chooses a product)."""
    data = call("search_products", {"query": query, "addressId": address_id()})
    out = []
    for rank, p in enumerate(data.get("products") or []):
        for v in p.get("variations") or []:
            if not v.get("isInStockAndAvailable", p.get("inStock")):
                continue
            price = (v.get("price") or {}).get("offerPrice") or (v.get("price") or {}).get("mrp")
            g = grams(v.get("quantityDescription"))
            out.append({"name": v.get("displayName") or p.get("displayName"), "brand": v.get("brandName") or p.get("brand"),
                        "pack": v.get("quantityDescription"), "price": price, "mrp": (v.get("price") or {}).get("mrp"),
                        "per_100": round(price / g * 100, 1) if price and g else None, "spinId": v.get("spinId"),
                        "skuId": v.get("skuId"), "rank": rank, "eta_min": (v.get("sla") or {}).get("value")})
    return out


def cart_items() -> list[dict[str, Any]]:
    """What is already in the user's Instamart cart, as update_cart items (so adding never removes anything)."""
    data = call("get_cart", {})
    found: list[dict[str, Any]] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if x.get("spinId") and (x.get("quantity") or x.get("qty")):
                found.append({"spinId": x["spinId"], "skuId": x.get("skuId"), "quantity": int(x.get("quantity") or x.get("qty"))})
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    has_items = bool(data.get("items")) and not data.get("cartAbsent")
    if has_items and len(found) < len(data.get("items") or []):  # could not read every existing line safely
        raise SwiggyError("Your Swiggy cart already has items I can't read clearly, so I won't touch it — "
                          "please add these yourself, or empty the cart in the Swiggy app and try again.")
    return found


def add_to_cart(new: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge into the current cart (existing items kept; same item → quantities added). Never checks out."""
    current = {i["spinId"]: i for i in cart_items()}
    for it in new:
        if it["spinId"] in current:
            current[it["spinId"]]["quantity"] += int(it.get("quantity") or 1)
        else:
            current[it["spinId"]] = {"spinId": it["spinId"], "skuId": it.get("skuId"), "quantity": int(it.get("quantity") or 1)}
    items = [{k: v for k, v in i.items() if v is not None} for i in current.values()]
    call("update_cart", {"selectedAddressId": address_id(), "items": items})
    after = call("get_cart", {})
    return {"items_in_cart": len(items), "cart": after}
