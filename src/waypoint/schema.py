"""Request validation against the contract's own schemas (openapi.json, a copy of docs/api/openapi.yaml)."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

import jsonschema

from .errors import ApiError


@lru_cache(maxsize=1)
def spec() -> dict:
    return json.loads(resources.files("waypoint").joinpath("openapi.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def validator(name: str) -> jsonschema.Draft202012Validator:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/components/schemas/{name}",
        "components": spec()["components"],
    }
    return jsonschema.Draft202012Validator(schema)


def check(name: str, body) -> None:
    """Raise ApiError('invalid') naming the first bad field; never echo the value."""
    errors = list(validator(name).iter_errors(body))
    if not errors:
        return
    err = jsonschema.exceptions.best_match(errors)
    path = [str(p) for p in err.absolute_path]
    if err.validator in ("additionalProperties", "unevaluatedProperties") and isinstance(err.instance, dict):
        allowed = set((err.schema or {}).get("properties", {}))
        extra = sorted(k for k in err.instance if k not in allowed)
        if extra:
            path.append(extra[0])
            return _raise(path, "unknown field")
    if err.validator == "required":
        missing = [r for r in err.validator_value if isinstance(err.instance, dict) and r not in err.instance]
        if missing:
            path.append(missing[0])
            return _raise(path, "required")
    err = _innermost(err)
    _raise([str(p) for p in err.absolute_path], _why(err))


def _innermost(err):
    """For a field that may be a value or null (oneOf), the error of the value's own rule."""
    while err.validator in ("oneOf", "anyOf") and err.context:
        real = [e for e in err.context if e.validator != "type"] or list(err.context)
        err = jsonschema.exceptions.best_match(real)
    return err


def _why(err) -> str:
    """Why a value was refused, with the limit; the value itself is never repeated (only its length)."""
    v, limit, inst = err.validator, err.validator_value, err.instance
    if v == "maxLength" and isinstance(inst, str):
        return f"{len(inst)} characters, at most {limit}"
    if v == "minLength" and isinstance(inst, str):
        return f"{len(inst)} characters, at least {limit}"
    if v == "maxItems" and isinstance(inst, list):
        return f"{len(inst)} entries, at most {limit}"
    if v == "minProperties":
        return "nothing to change"
    if v == "enum":
        return "one of " + ", ".join(str(x) for x in limit)
    if v == "pattern":
        return "not in the expected form"
    if v == "type":
        return f"must be {limit}" if isinstance(limit, str) else "wrong type"
    return f"not valid ({v})"


def limits() -> dict[str, int]:
    """The length limits of the free-text fields, from the contract, for help texts."""
    c = spec()["components"]["schemas"]
    q = c["Question"]["properties"]
    create = c["ItemCreate"]["properties"]
    return {
        "title": c["Title"]["maxLength"],
        "label": c["Label"]["maxLength"],
        "note": c["Note"]["maxLength"],
        "question": q["text"]["maxLength"],
        "option": q["options"]["items"]["maxLength"],
        "options": q["options"]["maxItems"],
        "recommendation": q["recommendation"]["oneOf"][0]["maxLength"],
        "url": c["Link"]["properties"]["url"]["maxLength"],
        "links": create["links"]["maxItems"],
        "waits_on": create["waits_on"]["maxItems"],
    }


def _raise(path: list[str], why: str) -> None:
    field = ".".join(path) if path else None
    raise ApiError("invalid", f"{field or 'body'}: {why}", field=field)
