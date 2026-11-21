"""Recipe → Swiggy Instamart cart: code picks packs + totals; cart only after yes; merge never removes; never checkout."""
import pytest

from app.chinku import executor, tools
from app.chinku.adapters import swiggy
from app.chinku.models import Plan, Step
from app.store.dynamo import MemoryStore, set_store

ME = "whatsapp:+919989683607"
OUT, CARDS, CALLS = [], [], []


@pytest.fixture(autouse=True)
def mem(monkeypatch):
    set_store(MemoryStore(path=None))
    OUT.clear(); CARDS.clear(); CALLS.clear()
    monkeypatch.setattr(swiggy, "connected", lambda: True)
    monkeypatch.setattr(swiggy, "address_id", lambda *a: "addr1")
    yield


def V(name, pack, price, rank=0, spin=None):
    return {"name": name, "brand": "", "pack": pack, "price": price, "mrp": price, "per_100": None, "spinId": spin or f"S-{name}-{pack}",
            "skuId": "K", "rank": rank, "eta_min": "7"}


CATALOG = {
    "basmati rice": [V("India Gate Basmati Rice", "1 kg", 133), V("India Gate Basmati Rice", "5 kg", 440), V("Rice Bran Oil", "1 l", 150, 1)],
    "chicken curry cut": [V("Licious Chicken Curry Cut", "450 g", 189), V("Licious Chicken Curry Cut", "1 kg", 380)],
    "biryani masala": [V("Shan Biryani Masala", "50 g", 85)],
    "saffron": [],
}


def test_code_picks_relevant_enough_cheapest_and_totals(monkeypatch):
    monkeypatch.setattr(swiggy, "search", lambda q: CATALOG.get(q, []))
    ctx = {"task": type("T", (), {"task_id": "i_s"})(), "card": lambda t, c: CARDS.append((t, c)), "step_id": "s2"}
    ings = [{"name": "Basmati rice", "search_query": "basmati rice", "grams": 500},
            {"name": "Chicken", "search_query": "chicken curry cut", "grams": 750},
            {"name": "Biryani masala", "search_query": "biryani masala", "grams": None},
            {"name": "Saffron", "search_query": "saffron"},
            {"name": "Salt", "search_query": "salt", "pantry": True}]
    out = tools.shop_list(ME, {"ingredients": ings}, ctx)
    picked = {r["ingredient"]: (r["pack"], r["price"]) for r in out["rows"]}
    assert picked["Basmati rice"] == ("1 kg", 133)          # enough (≥500 g) and cheapest; oil never picked for rice
    assert picked["Chicken"] == ("1 kg", 380)               # 450 g is not enough for 750 g
    assert out["total"] == 133 + 380 + 85                    # code adds
    assert out["missing"] == ["Saffron"] and out["skipped"] == ["Salt"]


def test_cart_add_waits_for_yes_merges_and_never_twice(monkeypatch):
    cart = {"items": [{"spinId": "EXISTING", "quantity": 2}], "cartAbsent": False}

    def fake_call(name, args):
        CALLS.append((name, args))
        if name == "get_cart":
            return cart
        if name == "update_cart":
            cart["items"] = args["items"]
            return {"ok": True}
        raise AssertionError(name)
    monkeypatch.setattr(swiggy, "call", fake_call)
    p = Plan(understood="add", confidence=0.95, steps=[Step(id="s1", tool="cart_add", needs_approval=False,
             inputs={"items": [{"spinId": "NEW1", "quantity": 1}, {"spinId": "EXISTING", "quantity": 1}], "rows": [], "total": 518})])
    monkeypatch.setattr("app.chinku.planner.plan", lambda *a, **k: p)
    executor.handle(ME, "add to cart", [], OUT.append, lambda t, c: CARDS.append((t, c)))
    assert not any(n == "update_cart" for n, _ in CALLS)      # nothing before yes, even though the plan forgot approval
    assert any("Needs you" in t and "Swiggy cart" in t for t, _ in CARDS)
    executor.handle(ME, "yes", [], OUT.append, lambda t, c: CARDS.append((t, c)))
    upd = [a for n, a in CALLS if n == "update_cart"]
    assert len(upd) == 1
    sent = {i["spinId"]: i["quantity"] for i in upd[0]["items"]}
    assert sent == {"EXISTING": 3, "NEW1": 1}                  # existing item kept (and topped up), nothing removed
    executor.handle(ME, "yes", [], OUT.append, lambda t, c: CARDS.append((t, c)))
    assert len([1 for n, _ in CALLS if n == "update_cart"]) == 1


def test_checkout_clear_and_payment_are_blocked_in_code():
    for name in ("checkout", "clear_cart", "get_payment_options", "confirm_order", "place_food_order"):
        with pytest.raises(PermissionError):
            swiggy.call(name, {})


def test_unreadable_existing_cart_is_never_overwritten(monkeypatch):
    monkeypatch.setattr(swiggy, "call", lambda name, args: {"items": [{"productName": "Milk", "count": 1}], "cartAbsent": False}
                        if name == "get_cart" else (_ for _ in ()).throw(AssertionError("must not update")))
    with pytest.raises(swiggy.SwiggyError, match="won't touch it"):
        swiggy.add_to_cart([{"spinId": "NEW1", "quantity": 1}])


def test_no_single_pack_big_enough_takes_several_and_short_query_retry(monkeypatch):
    cat = {"chicken curry cut": [V("Shah Fresh Chicken Curry Cut", "400 g", 119), V("Shah Fresh Chicken Curry Cut", "250 g", 90)],
           "mint leaves": [], "mint": [V("Mint (Pudina)", "100 g", 12)]}
    monkeypatch.setattr(swiggy, "search", lambda q: cat.get(q, []))
    ctx = {"task": type("T", (), {"task_id": "i_q"})(), "card": lambda t, c: CARDS.append((t, c)), "step_id": "s2"}
    out = tools.shop_list(ME, {"ingredients": [{"name": "Chicken", "search_query": "chicken curry cut", "grams": 800},
                                               {"name": "Mint leaves", "search_query": "mint leaves", "grams": 30}]}, ctx)
    rows = {r["ingredient"]: r for r in out["rows"]}
    assert rows["Chicken"]["pack"] == "400 g" and rows["Chicken"]["quantity"] == 2 and rows["Chicken"]["line_total"] == 238
    assert rows["Mint leaves"]["product"] == "Mint (Pudina)" and out["missing"] == []
    assert out["total"] == 238 + 12 and {i["spinId"]: i["quantity"] for i in out["items"]}[rows["Chicken"]["spinId"]] == 2


def test_leaves_never_become_seeds(monkeypatch):
    """Real case (Sun 01:55): 'coriander leaves' → fallback 'coriander' picked 'Coriander (Dhania) Whole' seeds."""
    cat = {"coriander leaves": [], "coriander": [V("KITCHEN GEMS Coriander (Dhania) Whole", "200 g", 9), V("Coriander Leaves (Kothimbir)", "100 g", 15, 1)]}
    monkeypatch.setattr(swiggy, "search", lambda q: cat.get(q, []))
    ctx = {"task": type("T", (), {"task_id": "i_c"})(), "card": lambda t, c: CARDS.append((t, c)), "step_id": "s2"}
    out = tools.shop_list(ME, {"ingredients": [{"name": "Coriander leaves", "search_query": "coriander leaves", "grams": 30}]}, ctx)
    assert out["rows"][0]["product"] == "Coriander Leaves (Kothimbir)" and "check" in out["rows"][0]["note"]


def test_food_search_tries_alternatives_filters_and_ranks_in_code(monkeypatch):
    menus = {"veg meals": {"items": [{"name": "Veg platter", "price": 399, "isVeg": True, "menu_item_id": "1", "restaurant_id": "r1", "restaurant_name": "A", "deliveryTimeMinutes": 35}]},
             "veg thali": {"items": [{"name": "Mini thali", "price": 149, "isVeg": True, "menu_item_id": "2", "restaurant_id": "r2", "restaurant_name": "B", "deliveryTimeMinutes": 30},
                                     {"name": "Chicken thali", "price": 179, "isVeg": False, "menu_item_id": "3", "restaurant_id": "r2", "restaurant_name": "B", "deliveryTimeMinutes": 30}]},
             "veg biryani": {"items": [{"name": "Veg biryani", "price": 189, "isVeg": True, "menu_item_id": "4", "restaurant_id": "r3", "restaurant_name": "C", "deliveryTimeMinutes": 55}]}}
    calls = []
    monkeypatch.setattr(swiggy, "call", lambda name, args, server="im": calls.append(args["query"]) or menus.get(args["query"], {"items": []}))
    ctx = {"task": type("T", (), {"task_id": "i_f"})(), "card": lambda t, c: CARDS.append((t, c)), "step_id": "s1"}
    out = tools.food_search(ME, {"query": "veg meals", "veg": True, "max_price": 200, "max_minutes": 40}, ctx)
    assert [r["dish"] for r in out["rows"]] == ["Mini thali"]                          # only veg, ≤ ₹200, ≤ 40 min
    whys = {e["item"].split(" · ")[0]: e["why"] for e in out["excluded"]}
    assert whys["Veg platter"].startswith("₹399") and whys["Chicken thali"] == "not veg" and "55 min" in whys["Veg biryani"]
    assert calls[:2] == ["veg meals", "veg thali"] and len(calls) <= 4                 # tried alternatives, at most 4 searches



ORIG_POST = swiggy._post  # captured at import, before the conftest guard replaces it


def test_429_is_retried_after_waiting(monkeypatch):
    import httpx
    posts, sleeps = [], []

    class R:
        def __init__(self, code):
            self.status_code, self.headers = code, {"retry-after": "2"}
            self.text = '{"jsonrpc":"2.0","id":5,"result":{"ok":true}}'
    monkeypatch.setattr(swiggy.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posts.append(1) or R(429 if len(posts) < 3 else 200))
    out, _ = ORIG_POST({"jsonrpc": "2.0", "id": 5, "method": "x"}, "tok", None)
    assert out["result"]["ok"] is True and len(posts) == 3 and len(sleeps) == 2 and all(s >= 1.5 for s in sleeps)


def test_search_cache_and_cooldown(monkeypatch):
    live = []
    monkeypatch.setattr(swiggy, "_call_live", lambda name, args, server: live.append(args["query"]) or {"products": [], "q": args["query"]})
    a = swiggy.call("search_products", {"query": "soap", "addressId": "x"})
    b = swiggy.call("search_products", {"query": "soap", "addressId": "x"})
    assert a == b and live == ["soap"]                                   # second identical search served from cache
    import httpx
    monkeypatch.setattr(swiggy.time, "sleep", lambda s: None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: type("R", (), {"status_code": 429, "headers": {}, "text": ""})())
    swiggy._cool["until"] = 0
    with pytest.raises(swiggy.SwiggyError, match="limiting requests"):
        ORIG_POST({"jsonrpc": "2.0", "id": 1}, "tok", None)
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1))
    with pytest.raises(swiggy.SwiggyError, match="try again in"):
        ORIG_POST({"jsonrpc": "2.0", "id": 1}, "tok", None)
    assert calls == []                                                   # cooling down: Swiggy is not called at all
    swiggy._cool["until"] = 0
