"""Regenerate checked-in JSON contracts; npm run types produces TypeScript."""

import json
from pathlib import Path

from entryplug_app.contracts import definition_schema
from entryplug_server.schemas import openapi

root = Path(__file__).resolve().parents[1] / "docs" / "api"
root.mkdir(parents=True, exist_ok=True)
for name, document in (("openapi", openapi()), ("mission.schema", definition_schema())):
    (root / (name + ".json")).write_text(json.dumps(document, indent=2) + "\n")
