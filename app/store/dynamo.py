"""State store. Same interface for local (JSON-file-backed memory) and DynamoDB.

Tables: tasks, calls, contacts, events, seen (idempotency keys).
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

TABLES = ("tasks", "calls", "contacts", "events", "seen")


class MemoryStore:
    """In-memory store, persisted to a JSON file so a restart resumes (local dev / fallback)."""

    def __init__(self, path: str | None = "data/store.json") -> None:
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.data: dict[str, dict[str, Any]] = {t: {} for t in TABLES}
        if self.path and self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            for t in TABLES:
                self.data[t] = loaded.get(t, {})

    def _flush(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    def put(self, table: str, key: str, item: dict[str, Any]) -> None:
        with self.lock:
            item = {**item, "updated_at": time.time()}
            self.data[table][key] = item
            self._flush()

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        with self.lock:
            v = self.data[table].get(key)
            return json.loads(json.dumps(v, default=str)) if v is not None else None

    def scan(self, table: str, **eq: Any) -> list[dict[str, Any]]:
        with self.lock:
            rows = [json.loads(json.dumps(v, default=str)) for v in self.data[table].values()]
        return [r for r in rows if all(r.get(k) == v for k, v in eq.items())]

    def claim(self, key: str) -> bool:
        """Idempotency: returns True the first time a key is seen, False on duplicates."""
        with self.lock:
            if key in self.data["seen"]:
                return False
            self.data["seen"][key] = {"ts": time.time()}
            self._flush()
            return True

    def add_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self.lock:
            k = f"{time.time():.6f}-{len(self.data['events'])}"
            self.data["events"][k] = {"task_id": task_id, "ts": time.time(), "kind": kind, "payload": payload}
            self._flush()

    def events(self, task_id: str) -> list[dict[str, Any]]:
        return sorted(self.scan("events", task_id=task_id), key=lambda e: e["ts"])


_store: MemoryStore | None = None


def store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore(os.environ.get("CHINKU_STORE_PATH", "data/store.json") or None)
    return _store


def set_store(s: MemoryStore) -> None:
    global _store
    _store = s
