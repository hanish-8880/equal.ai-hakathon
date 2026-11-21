"""State store. Same interface for local (JSON-file-backed memory) and DynamoDB.

Tables: tasks, calls, contacts, events, seen (idempotency keys).
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

TABLES = ("tasks", "calls", "contacts", "events", "seen", "jobs")


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

    def delete(self, table: str, key: str) -> None:
        with self.lock:
            self.data[table].pop(key, None)
            self._flush()

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


def aws_env() -> None:
    """boto3 reads keys from the process env; copy them from .env-backed settings if present."""
    from app.config import settings
    s = settings()
    if s.aws_access_key_id and not os.environ.get("AWS_ACCESS_KEY_ID"):
        os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"] = s.aws_access_key_id, s.aws_secret_access_key
        if s.aws_session_token:
            os.environ["AWS_SESSION_TOKEN"] = s.aws_session_token
    os.environ.setdefault("AWS_DEFAULT_REGION", s.aws_region)


class DynamoStore:
    """DynamoDB, one table per kind (chinku_tasks, chinku_calls, ...). Key 'k'; the item is kept as JSON in 'doc'
    (avoids Decimal conversion). scan() filters in Python — fine at hackathon scale."""

    def __init__(self, prefix: str = "chinku_", region: str = "ap-south-1") -> None:
        import boto3
        self.db = boto3.resource("dynamodb", region_name=region)
        self.t = {name: self.db.Table(prefix + name) for name in TABLES}

    @staticmethod
    def _enc(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, default=str)

    def put(self, table: str, key: str, item: dict[str, Any]) -> None:
        item = {**item, "updated_at": time.time()}
        extra = {"task_id": str(item["task_id"])} if item.get("task_id") is not None else {}
        self.t[table].put_item(Item={"k": key, "doc": self._enc(item), **extra})

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        r = self.t[table].get_item(Key={"k": key}, ConsistentRead=True).get("Item")
        return json.loads(r["doc"]) if r else None

    def delete(self, table: str, key: str) -> None:
        self.t[table].delete_item(Key={"k": key})

    def _all(self, table: str, **kw: Any) -> list[dict[str, Any]]:
        out, start = [], None
        while True:
            args = {**kw, **({"ExclusiveStartKey": start} if start else {}), "ConsistentRead": True}
            r = self.t[table].scan(**args)
            out += [json.loads(i["doc"]) for i in r.get("Items", [])]
            start = r.get("LastEvaluatedKey")
            if not start:
                return out

    def scan(self, table: str, **eq: Any) -> list[dict[str, Any]]:
        return [r for r in self._all(table) if all(r.get(k) == v for k, v in eq.items())]

    def claim(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.t["seen"].put_item(Item={"k": key, "doc": self._enc({"ts": time.time()})},
                                    ConditionExpression="attribute_not_exists(k)")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def add_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        import uuid
        k = f"{time.time():.6f}-{uuid.uuid4().hex[:6]}"
        self.put("events", k, {"task_id": task_id, "ts": time.time(), "kind": kind, "payload": payload})

    def events(self, task_id: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Attr
        return sorted(self._all("events", FilterExpression=Attr("task_id").eq(task_id)), key=lambda e: e["ts"])


def create_dynamo_tables(prefix: str = "chinku_", region: str = "ap-south-1") -> list[str]:
    """Create missing tables without dynamodb:ListTables (the EC2 role only has access to chinku_* tables)."""
    import boto3
    c = boto3.client("dynamodb", region_name=region)
    made = []
    for name in TABLES:
        try:
            c.create_table(TableName=prefix + name, BillingMode="PAY_PER_REQUEST",
                           AttributeDefinitions=[{"AttributeName": "k", "AttributeType": "S"}],
                           KeySchema=[{"AttributeName": "k", "KeyType": "HASH"}])
            made.append(prefix + name)
        except c.exceptions.ResourceInUseException:
            pass
    for name in made:
        c.get_waiter("table_exists").wait(TableName=name)
    return made


_store: MemoryStore | DynamoStore | None = None


def store() -> MemoryStore | DynamoStore:
    global _store
    if _store is None:
        from app.config import settings
        s = settings()
        if (os.environ.get("STORE_BACKEND") or s.store_backend).lower() == "dynamo":
            aws_env()
            _store = DynamoStore(s.ddb_table_prefix, s.aws_region)
        else:
            _store = MemoryStore(os.environ.get("CHINKU_STORE_PATH", "data/store.json") or None)
    return _store


def set_store(s: "MemoryStore | DynamoStore") -> None:
    global _store
    _store = s
