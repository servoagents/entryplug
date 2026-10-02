"""Generate the versioned API document from canonical domain contracts."""

from typing import Any

from entryplug_app.contracts import Activity, Health, MissionLifecycle, definition_schema
from entryplug_app.dtos import response_schemas

# Route metadata is also used for dispatching application commands.
COMMANDS = {
    ("POST", "approvals/{approval_id}/decision"): "approval.decision",
    ("POST", "missions"): "mission.create",
    ("PATCH", "missions/{mission_id}"): "mission.update",
    ("POST", "missions/{mission_id}/runs"): "mission.start",
    ("POST", "runs/{run_id}/pause"): "run.pause",
    ("POST", "runs/{run_id}/resume"): "run.resume",
    ("POST", "runs/{run_id}/stop"): "run.stop",
    ("POST", "runs/{run_id}/inputs"): "run.input",
    ("POST", "operations"): "operation.create",
    ("POST", "operations/{operation_id}/cancel"): "operation.cancel",
    ("POST", "attachments"): "attachment.create",
    ("DELETE", "attachments/{attachment_id}"): "attachment.revoke",
    ("POST", "alerts/{alert_id}/ack"): "alert.ack",
}


def openapi() -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for (method, route), action in COMMANDS.items():
        parameters = [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
            }
        ]
        for part in route.split("/"):
            if part.startswith("{"):
                parameters.append(
                    {
                        "name": part[1:-1],
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                )
        if method == "PATCH":
            parameters.append(
                {"name": "If-Match", "in": "header", "required": True, "schema": {"type": "string"}}
            )
        schema = (
            {"$ref": "#/components/schemas/MissionDefinition"}
            if action == "mission.create"
            else {"type": "object"}
        )
        paths.setdefault("/v1/" + route, {})[method.lower()] = {
            "operationId": action.replace(".", "_"),
            "parameters": parameters,
            "requestBody": {"content": {"application/json": {"schema": schema}}},
            "responses": {
                "202": {"description": "Accepted; inspect the returned ID"},
                "400": {"description": "Typed validation error"},
                "409": {"description": "State or idempotency conflict"},
            },
        }
    for route in (
        "health",
        "providers",
        "simulations",
        "snapshot",
        "bodies",
        "capabilities",
        "topology",
        "agents",
        "connections",
        "missions",
        "missions/{mission_id}",
        "runs/{run_id}",
        "operations/{operation_id}",
        "alerts",
        "evidence/{evidence_id}",
        "evidence/{evidence_id}/content",
        "events",
    ):
        parameters = [
            {"name": p[1:-1], "in": "path", "required": True, "schema": {"type": "string"}}
            for p in route.split("/")
            if p.startswith("{")
        ]
        paths.setdefault("/v1/" + route, {})["get"] = {
            "operationId": "get_" + route.replace("/", "_").replace("{", "").replace("}", ""),
            "parameters": parameters,
            "responses": {"200": {"description": "Current public state"}},
        }
    for route in ("profiles", "profiles/{profile_id}/models", "session", "openapi.json"):
        paths.setdefault("/v1/" + route, {})["get"] = {
            "responses": {"200": {"description": "Current authenticated state"}}
        }
    for method, route in (
        ("post", "missions/validate"),
        ("post", "connections"),
        ("delete", "connections/{connection_id}"),
        ("post", "connections/{connection_id}/test"),
        ("post", "connections/{connection_id}/retry"),
        ("post", "profiles"),
        ("post", "auth/chatgpt/start"),
        ("post", "auth/logout"),
        ("post", "session/bootstrap"),
        ("post", "session/exchange"),
        ("post", "demo/step"),
        ("post", "simulations"),
        ("post", "simulations/{simulation_id}/control"),
    ):
        paths.setdefault("/v1/" + route, {})[method] = {
            "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            "responses": {"200": {"description": "Accepted; inspect current state"}},
        }
    response_types = {
        "snapshot": "Snapshot",
        "missions/{mission_id}": "MissionRecord",
        "runs/{run_id}": "MissionRun",
        "operations/{operation_id}": "OperationRecord",
    }
    for route, name in response_types.items():
        paths["/v1/" + route]["get"]["responses"]["200"]["content"] = {
            "application/json": {"schema": {"$ref": "#/components/schemas/" + name}}
        }
    paths["/v1/events"]["get"]["responses"] = {
        "200": {
            "description": "Durable entryplug events and ephemeral activity; bounded replay",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        "410": {"description": "replay_expired: refresh snapshot before reconnecting"},
    }
    for route, methods in paths.items():
        for method, operation in methods.items():
            if method != "get" and "parameters" not in operation:
                operation["parameters"] = [
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": route
                        not in {
                            "/v1/session/bootstrap",
                            "/v1/session/exchange",
                            "/v1/missions/validate",
                            "/v1/connections/{connection_id}/test",
                        },
                        "schema": {"type": "string"},
                    }
                ]
            declared = {p["name"] for p in operation.get("parameters", [])}
            for part in route.split("/"):
                if part.startswith("{") and part[1:-1] not in declared:
                    operation.setdefault("parameters", []).append(
                        {
                            "name": part[1:-1],
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    )
    return {
        "openapi": "3.1.0",
        "info": {"title": "Entryplug mission service", "version": "1"},
        "paths": paths,
        "security": [{"LocalBearer": []}],
        "components": {
            "securitySchemes": {"LocalBearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                **response_schemas(),
                "MissionDefinition": definition_schema(),
                "MissionLifecycle": {"type": "string", "enum": list(MissionLifecycle)},
                "Activity": {"type": "string", "enum": list(Activity)},
                "Health": {"type": "string", "enum": list(Health)},
            },
        },
    }
