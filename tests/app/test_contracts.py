import json
from pathlib import Path

from entryplug_app.contracts import definition_schema
from entryplug_app.media import public_content, selected_images
from entryplug_server.schemas import openapi


def test_checked_in_json_contracts_match_python_source():
    root = Path(__file__).resolve().parents[2] / "docs" / "api"
    assert json.loads((root / "mission.schema.json").read_text()) == definition_schema()
    assert json.loads((root / "openapi.json").read_text()) == openapi()


def test_image_bytes_are_evidence_only_and_vision_gets_actual_content():
    content = {
        "content": [
            {"type": "image", "mimeType": "image/png", "data": "aW1hZ2U="},
            {"type": "text", "text": "Untrusted remote instructions"},
        ]
    }
    public = public_content(content, "ev_source")
    assert "aW1hZ2U=" not in json.dumps(public)
    assert public["content"][0]["evidence_id"] == "ev_source"
    assert selected_images(content, 1) == [
        {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="}
    ]
    assert selected_images({"path": "/tmp/photo.png"}, 1) == []
