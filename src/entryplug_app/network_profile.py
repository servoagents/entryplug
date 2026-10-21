"""Operator-owned network bindings; discovery never grants authority or semantics."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

from entryplug_app.contracts import AppError, canonical


def concrete_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(c in value for c in "*+$?#\x00")
        or any(c.isspace() for c in value)
        or any(p in {"", ".", ".."} for p in value.split("/"))
    ):
        raise ValueError("Use concrete keys/topics without wildcards or empty segments")
    return value


def bounded_number(value: Any, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Expected a finite number between {low} and {high}")
    return float(value)


def action_schema(schema: Any) -> None:
    """Reject unsupported constraints instead of silently weakening device limits."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("Action input_schema must be an object")
    if set(schema) - {"type", "properties", "required", "additionalProperties", "description"}:
        raise ValueError("Unsupported action schema keyword")
    properties, required = schema.get("properties"), schema.get("required", [])
    if (
        not isinstance(properties, dict)
        or len(properties) > 16
        or schema.get("additionalProperties") is not False
        or not isinstance(required, list)
        or any(not isinstance(k, str) or k not in properties for k in required)
    ):
        raise ValueError("Actions require a closed object with at most 16 scalar properties")
    for name, item in properties.items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name) or not isinstance(item, dict):
            raise ValueError("Invalid action property")
        kind = item.get("type")
        allowed = {"type", "description", "enum"}
        if kind in {"number", "integer"}:
            allowed |= {"minimum", "maximum"}
            low = bounded_number(item.get("minimum"), -1e12, 1e12)
            bounded_number(item.get("maximum"), low, 1e12)
        elif kind == "string":
            allowed.add("maxLength")
            if type(item.get("maxLength")) is not int or not 1 <= item["maxLength"] <= 1024:
                raise ValueError("String arguments require maxLength between 1 and 1024")
        elif kind != "boolean":
            raise ValueError("Action arguments support bounded numbers, strings and booleans")
        if set(item) - allowed:
            raise ValueError("Unsupported action property constraint")
        if "enum" in item and (
            not isinstance(item["enum"], list) or not 1 <= len(item["enum"]) <= 32
        ):
            raise ValueError("Enums require 1–32 values")
        if "enum" in item:
            from entryplug_agents.native import validate_arguments

            for value in item["enum"]:
                validate_arguments(value, {k: v for k, v in item.items() if k != "enum"})


def validate_network(config: dict[str, Any]) -> dict[str, Any]:
    """Validate before opening a socket, persisting a profile, or exposing tools."""
    try:
        kind, endpoint, profile = config["kind"], config["url"], config.get("profile")
        if kind == "mqtt":
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"mqtt", "mqtts"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("Use a credential-free mqtt(s)://host:port endpoint")
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError("Invalid MQTT port")
        elif kind == "zenoh":
            # TLS configuration/identity lives in the referenced native config, never the profile.
            if not re.fullmatch(r"(?:tcp|tls)/[a-zA-Z0-9.\[\]:_-]+:[0-9]{1,5}", endpoint):
                raise ValueError("Use an explicit tcp/host:port or tls/host:port Zenoh endpoint")
            if not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535:
                raise ValueError("Invalid Zenoh port")
        else:
            raise ValueError("Unsupported native network")
        if not isinstance(profile, dict) or set(profile) - {"observations", "actions", "reply_key"}:
            raise ValueError("Provide an explicit observations/actions profile")
        if len(canonical(profile).encode()) > 32768:
            raise ValueError("Network profile exceeds 32 KiB")
        observations, actions = profile.get("observations", []), profile.get("actions", [])
        if (
            not isinstance(observations, list)
            or not isinstance(actions, list)
            or not 1 <= len(observations) + len(actions) <= 32
        ):
            raise ValueError("Select 1–32 named observations/actions")
        names, keys = set(), set()
        for items, is_action in ((observations, False), (actions, True)):
            for item in items:
                fields = {"name", "key", "description"} | (
                    {"input_schema", "timeout_s"} if is_action else {"max_age_s"}
                )
                if not isinstance(item, dict) or set(item) - fields:
                    raise ValueError("Unsupported network binding field")
                name = item.get("name")
                if not isinstance(name, str) or not re.fullmatch(
                    r"[a-zA-Z][a-zA-Z0-9_.-]{0,99}", name
                ):
                    raise ValueError("Each binding requires a capability name")
                if name in names or name == "alerts.emit" or name.startswith("entryplug_"):
                    raise ValueError(
                        "Capability names must be unique and cannot use reserved names"
                    )
                names.add(name)
                key = concrete_key(item.get("key"))
                if key in keys:
                    raise ValueError("Use distinct observation and action keys")
                keys.add(key)
                if (
                    not isinstance(item.get("description"), str)
                    or not 1 <= len(item["description"]) <= 1000
                ):
                    raise ValueError("Describe each binding's meaning and units")
                bounded_number(item.get("timeout_s" if is_action else "max_age_s", 5), 0.1, 60)
                if is_action:
                    action_schema(item.get("input_schema"))
        if kind == "mqtt" and actions:
            if concrete_key(profile.get("reply_key")) in keys:
                raise ValueError("The MQTT reply topic must be separate from device bindings")
        elif "reply_key" in profile:
            raise ValueError("reply_key is only used for MQTT actions")
    except (ValueError, TypeError, KeyError, AppError) as error:
        raise AppError("validation_error", str(error)) from error
    return profile


def normalize_endpoint(kind: str, endpoint: str) -> str:
    """Keep equivalent URL spellings from allocating separate action owners."""
    if kind == "mqtt":
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port or (8883 if parsed.scheme == "mqtts" else 1883)
        return f"{parsed.scheme}://{host}:{port}"
    return endpoint.lower()
