"""Declarative settlement basis shared by request and terminal-response paths."""

from __future__ import annotations

import json
import math
from typing import Any


def _micro(value: float, unit_micro: int) -> int:
    raw = round(float(value) * int(unit_micro), 9)
    whole = int(raw)
    return whole + 1 if raw > whole else whole


def _path(value: object, dotted: str) -> object:
    current = value
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def request_evidence(
    query_items: list[tuple[str, str]], body: bytes, *, path_names: set[str] | None = None,
) -> dict:
    """Return the provider-facing request values needed to replay a declarative price table."""
    query: dict[str, object] = {}
    for name, value in query_items:
        previous = query.get(name)
        if previous is None:
            query[name] = value
        elif isinstance(previous, list):
            previous.append(value)
        else:
            query[name] = [previous, value]
    try:
        parsed = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    path_names = path_names or set()
    path = {name: query.pop(name) for name in tuple(query) if name in path_names}
    return {"queryParams": query, "pathParams": path, "body": parsed}


def _with_defaults(request: dict, input_schema: dict) -> dict:
    out = {"queryParams": dict(request.get("queryParams") or {}),
           "pathParams": dict(request.get("pathParams") or {}),
           "body": request.get("body") if isinstance(request.get("body"), dict) else {}}
    out["body"] = dict(out["body"])

    def apply(block: dict, values: dict) -> None:
        for name, spec in block.items():
            if not isinstance(spec, dict):
                continue
            if name not in values and "default" in spec:
                values[name] = spec["default"]
            if name in values and isinstance(values[name], str):
                try:
                    if spec.get("type") == "integer":
                        values[name] = int(values[name])
                    elif spec.get("type") == "number":
                        values[name] = float(values[name])
                    elif spec.get("type") == "boolean" and values[name].lower() in ("true", "false"):
                        values[name] = values[name].lower() == "true"
                except ValueError:
                    pass
            nested = spec.get("properties")
            if isinstance(nested, dict) and isinstance(values.get(name), dict):
                apply(nested, values[name])

    for location in ("queryParams", "pathParams", "body"):
        block = input_schema.get(location)
        if isinstance(block, dict):
            if isinstance(block.get("properties"), dict):
                block = block["properties"]
            apply(block, out[location])
    return out


def table_amount_micro(cost: dict, request: dict, input_schema: dict, unit_micro: int) -> int:
    """Evaluate the frozen first-match table, falling back to its explicit global upper bound."""
    values = _with_defaults(request, input_schema)
    for row in cost.get("table") or []:
        when = row.get("when") or {}
        if all(_path(values, field) == expected for field, expected in when.items()):
            multiplier = _path(values, row["times"]) if row.get("times") else 1
            if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool):
                break
            return _micro(float(row["value"]) * float(multiplier), unit_micro)
    return _micro(float(cost["fallback"]["value"]), unit_micro)


def derive_basis(
    cost: dict, *, request: dict, input_schema: dict, unit_micro: int,
    terminal: bool, response_estimate_micro: int = 0,
) -> dict:
    """Freeze when and how a reserved call will settle using catalog data only."""
    if cost.get("table") or cost.get("settle") == "usage":
        fallback = _micro(float(cost["fallback"]["value"]), unit_micro)
        if cost.get("settle") == "usage":
            amount = {"kind": "usage", **dict(cost["usage"])}
            reserve = fallback
        else:
            amount = {"kind": "table", "cost": cost, "input": input_schema,
                      "request": request, "unit_micro": unit_micro}
            reserve = table_amount_micro(cost, request, input_schema, unit_micro)
        return {"when": "terminal" if terminal else "response", "amount": amount,
                "fallback_micro": fallback,
                "reserve_micro": reserve}
    return {
        "when": "response",
        "amount": {"kind": "observed"},
        "fallback_micro": max(0, int(response_estimate_micro)),
        "reserve_micro": max(0, int(response_estimate_micro)),
    }


def settle(basis: dict, evidence: dict[str, Any]) -> int:
    """Resolve one frozen basis to raw integer micro-USD without moving ledger money."""
    amount = basis.get("amount") or {}
    kind = amount.get("kind")
    if kind == "table":
        return table_amount_micro(
            amount["cost"], amount["request"], amount["input"], int(amount["unit_micro"]))
    if kind == "usage":
        value = _path(evidence.get("terminal"), str(amount.get("path") or ""))
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
                and value >= 0:
            if amount.get("unit") == "usd":
                return _micro(float(value), 1_000_000)
    if kind == "observed":
        observed = evidence.get("observed_micro")
        if isinstance(observed, int) and not isinstance(observed, bool) and observed >= 0:
            return observed
    return max(0, int(basis.get("fallback_micro") or 0))
