"""Read-only probe of Swiggy Food search_menu + Dineout search/slots response shapes (runs on the server)."""
import json
from datetime import datetime, timedelta, timezone

from app.chinku.adapters import swiggy

swiggy.ALLOWED["dineout"] = {"search_restaurants_dineout", "get_available_slots", "get_restaurant_details"}
aid = swiggy.address_id("food")
m = swiggy.call("search_menu", {"query": "veg meals", "addressId": aid}, "food")
print("MENU keys:", list(m.keys()) if isinstance(m, dict) else type(m))
print(json.dumps(m, ensure_ascii=False)[:2200])
lat, lon = 17.4931, 78.4054  # Kukatpally
d = swiggy.call("search_restaurants_dineout", {"query": "biryani", "latitude": lat, "longitude": lon}, "dineout")
print("\nDINEOUT keys:", list(d.keys()) if isinstance(d, dict) else type(d)); print(json.dumps(d, ensure_ascii=False)[:1500])
import re
ids = re.findall(r"\(ID: (\d+)\)", d.get("text", ""))
if ids:
    rid = ids[0]
    day = (datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(days=1)).strftime("%Y-%m-%d")
    s = swiggy.call("get_available_slots", {"restaurantId": rid, "date": day, "latitude": lat, "longitude": lon}, "dineout")
    print("\nSLOTS keys:", list(s.keys()) if isinstance(s, dict) else type(s)); print(json.dumps(s, ensure_ascii=False)[:2000])
