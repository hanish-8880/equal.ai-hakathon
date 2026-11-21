"""List Swiggy MCP tools + input schemas for /food and /dineout (read-only; runs where the tokens are: the server).
Run: python -m scripts.swiggy_tools food|dineout"""
import json
import sys

from app.chinku.adapters import swiggy

server = sys.argv[1] if len(sys.argv) > 1 else "food"
tok = swiggy._token()
out, sid = swiggy._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "chinku", "version": "0.1"}}}, tok, None, server)
swiggy._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, tok, sid, server)
res, _ = swiggy._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, tok, sid, server)
for t in res["result"]["tools"]:
    print("==", t["name"], "|", (t.get("description") or "")[:420].replace("\n", " "))
    print("  ", json.dumps(t.get("inputSchema", {}).get("properties", {}), ensure_ascii=False)[:700], "req:", t.get("inputSchema", {}).get("required"))
