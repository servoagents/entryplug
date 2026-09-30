"""Keep selected image bytes in evidence, out of snapshots and event streams."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

ALLOWED_IMAGES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def public_content(value: Any, evidence_id: str) -> Any:
    if isinstance(value, list):
        return [public_content(item, evidence_id) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "image" and isinstance(value.get("data"), str):
            return {k: v for k, v in value.items() if k != "data"} | {
                "evidence_id": evidence_id,
                "encoding": "base64",
                "sha256": hashlib.sha256(value["data"].encode()).hexdigest(),
            }
        return {k: public_content(v, evidence_id) for k, v in value.items()}
    return value


def selected_images(value: Any, limit: int) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []

    def visit(item: Any) -> None:
        if len(images) >= limit:
            return
        if isinstance(item, dict):
            mime = item.get("mimeType", item.get("mime_type"))
            if item.get("type") == "image" and mime in ALLOWED_IMAGES:
                data = item.get("data", "")
                if not isinstance(data, str) or len(data) > 262144:
                    return
                try:
                    base64.b64decode(data, validate=True)
                except ValueError:
                    return
                images.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
            else:
                for child in item.values():
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return images
