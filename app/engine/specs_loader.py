"""Load spec YAMLs + tiny template / arithmetic helpers (deterministic, no LLM)."""
import ast
import operator
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

SPEC_DIR = Path(__file__).parent / "specs"
STEP_TYPES = {"call", "wait_for", "email", "schedule", "reply", "compute", "fetch_email", "make_doc", "job_op"}


class SpecError(ValueError):
    pass


def _check(spec: dict[str, Any], fname: str) -> dict[str, Any]:
    for k in ("name", "description", "inputs", "steps"):
        if k not in spec:
            raise SpecError(f"{fname}: missing '{k}'")
    ids = set()
    for st in spec["steps"]:
        if st.get("type") not in STEP_TYPES:
            raise SpecError(f"{fname}: step {st.get('id')} has unknown type {st.get('type')}")
        if st["id"] in ids:
            raise SpecError(f"{fname}: duplicate step id {st['id']}")
        ids.add(st["id"])
        if st["type"] == "call":
            for k in ("to", "goal"):
                if k not in st:
                    raise SpecError(f"{fname}: call step {st['id']} missing '{k}'")
            if "must_collect" not in st and "must_collect_from" not in st:
                raise SpecError(f"{fname}: call step {st['id']} needs must_collect or must_collect_from")
            for k in ():
                if k not in st:
                    raise SpecError(f"{fname}: call step {st['id']} missing '{k}'")
            missing = [f for f in st.get("must_collect", []) if f not in spec.get("result_schema", {})]
            if missing:
                raise SpecError(f"{fname}: must_collect not in result_schema: {missing}")
    return spec


@lru_cache
def load_specs() -> dict[str, dict[str, Any]]:
    out = {}
    for p in sorted(SPEC_DIR.glob("*.yaml")):
        spec = _check(yaml.safe_load(p.read_text(encoding="utf-8")), p.name)
        out[spec["name"]] = spec
    return out


def catalog() -> list[dict[str, Any]]:
    return [{"name": s["name"], "description": s["description"], "route_examples": s.get("route_examples", []),
             "inputs": s["inputs"]} for s in load_specs().values() if not s.get("internal")]


_TPL = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def _lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """Fill {inputs.x} / {item.y} placeholders. A whole-string placeholder keeps its type."""
    if isinstance(value, str):
        m = _TPL.fullmatch(value)
        if m:
            return _lookup(ctx, m.group(1))
        return _TPL.sub(lambda mm: "" if _lookup(ctx, mm.group(1)) is None else str(_lookup(ctx, mm.group(1))), value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def arith(expr: str, names: dict[str, Any]) -> float | None:
    """Safe + - * / over named numbers. Returns None if any needed number is missing."""
    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.Name):
            v = names.get(n.id)
            if v is None or isinstance(v, bool):
                raise KeyError(n.id)
            return float(v)
        raise SpecError(f"unsupported expression: {ast.dump(n)}")
    try:
        return ev(ast.parse(expr, mode="eval"))
    except (KeyError, ValueError, TypeError):
        return None
