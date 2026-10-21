# Native network embodiment — 21 October 2026

MQTT and Zenoh can now join the resident service directly. Operator-provided
profiles bind exact network routes to named observations and bounded actions.
Agents borrow those capabilities through the existing Session, durable operation
ledger, and HTTP/MCP/A2A interfaces. The generic ROS and Home Assistant paths
retain their existing providers and task contracts.

External attachments now honor an `approve` subset, defaulting to physical
actions. The original implementation silently ignored that field; regression
tests reproduced writes before owner approval. Admission and dispatch now check
the grant, approval, runtime and arguments. Revocation invalidates pending work
and requests cancellation of dispatched operations. Owners can explicitly
choose autonomous access to selected actions.

Native request/reply preserves device provenance and uncertainty. Retained MQTT
state cannot establish current readiness; broker acknowledgements cannot confirm
application. Wrong, missing or canceled replies inhibit further writes. The
application ledger retains that inhibition across reconnect/restart. Local action
routes have one configured owner while connected or unresolved; a cleanly
settled disconnect supports deliberate replacement. Equivalent MQTT URL spellings
share the ownership check.

The console offers native setup, a device-profile editor and external-action
approvals. The existing Zenoh observation bridge remains selectable. Runnable
connection payloads and the wire/credential contract are in the
[network guide](../network-embodiment.md).

Validation: **580 Python tests passed, zero skipped**, plus **11 source browser
journeys**, **11 final installed-wheel browser journeys**, **4 installed native
MQTT/Zenoh transport and ACL checks**, and **3 UI unit tests**. TypeScript/Vite,
changed Python lint/format, strict typing of six changed modules, dependency
checks and the seven-asset wheel check pass. The native checks exercise real
transports and native ACL refusal using simulated devices.

One concurrent validation attempt had 579 passes and a five-second rover-test
timeout. Its ledger shows delayed progress without an approval wait or network
action. Contention is plausible but not established as the cause. The unchanged
tests and deadlines passed in the sequential run; the failure and ledger are
retained. No timing threshold was relaxed. A pre-existing Authlib deprecation
warning remains.

[Exact commands, versions, source hashes, wheel identity and limits](evidence/network-embodiment/validation.json)
are retained alongside the test logs. Work remains uncommitted. Hosted CI was
updated but has not been run. Device-specific integration, production TLS/identity,
physical hardware, multi-host qualification and safe reconciliation after unknown
effects remain open, along with the older distributed embodiment release gates.
