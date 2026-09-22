# Contract examples — not software results

Every ID, value and outcome here is invented to illustrate the proposed interfaces. None is a measurement or a deployed endpoint. The JSON examples are **application data**, not complete MCP JSON-RPC or A2A transport messages. SDKs supply those envelopes.

- `visual_reach.goal.json`: exactly the goal fields in `../interfaces/VisualReach.action`; empty handle requests automatic preparation.
- `visual_reach.result.json`: exactly the result fields. Pixel error is agent-visible; private evaluator millimeters are deliberately absent.
- `visual_reach.feedback.json`: one compact public phase update.
- `a2a.data.json`: the application object placed inside an A2A DataPart by the pinned SDK. It is not an AgentSkill and does not invent SDK fields.
- `harbor.initial.toml`: starting numerical profile, not calibrated values or a full deployment language.

Client runners add request and runtime IDs. A model supplies the task arguments; it does not need to construct UUIDs or poll motion telemetry. A repeated start uses the same request ID and input, while a genuinely new target request gets a new ID.

`VisualReach.action` is the sole typed source for Harbor. These examples are fixtures: regenerate/check them when the action changes. They are not another schema authority. Core-only test capabilities may provide typed schemas directly without this ROS interface.

The fixture still requires real ROSIDL compilation and SDK interoperability tests.
