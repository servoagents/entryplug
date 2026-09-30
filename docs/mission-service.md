# Resident missions

The service owns missions and Sessions. The CLI, browser, Python client, MCP,
and A2A use that owner. Closing an observer leaves the mission running.
The original laboratory commands retain their existing behavior.

## Install and run

Python 3.12+ is required. Install a built wheel with its UI extra:

```bash
python -m pip install 'dist/entryplug-0.0.1-py3-none-any.whl[ui]'
entryplug serve --workspace default
```

In another terminal, `entryplug ui --workspace default` opens the console using
a one-use local sign-in. If no service exists, `ui` starts one **in the foreground**.
Closing that terminal closes its owner. Ordinary client commands never start a
hidden process. Use `--no-demo` on `serve` to exclude the simulated body.

The four workflows are overview/embark, mission operation, body/topology, and
connections. The inbox is persistent. Graph information also has a searchable
keyboard-accessible table. Dragged positions remain stable across updates;
layers and capability groups can be hidden without changing body permissions.

## Use a mission

This example needs no model account or hardware:

```bash
entryplug mission validate examples/watch-camera.toml --json
entryplug mission create examples/watch-camera.toml --json
# Use mission_id from create, then run_id from start:
entryplug mission start <mission-id> --json
entryplug mission tail <run-id> --json
# A separate terminal advances the labelled SIMULATED source:
entryplug demo sequence --json
entryplug mission status <run-id> --json
entryplug mission pause <run-id>
entryplug mission resume <run-id>
entryplug mission stop <run-id>
entryplug alert list --json
```

`tail --json` is NDJSON; Ctrl-C disconnects only that observer. The sequence
distinguishes sustained presence, two people, reentry, detector epochs,
source loss, recovery, and stale data. It is labelled data, **not a real person
detector or video source**. A watch returns to waiting after each bounded turn.
There are no inference calls while an event-driven mission waits.

The GUI can launch the same template, load a UTF-8 instruction file, edit
instructions for future turns, pause/resume/stop, inspect evidence, decide
pending approvals, and acknowledge alerts. Closing it does not acknowledge or
deliver anything implicitly. The inbox itself is the initial delivery sink.

Files are exchange formats. `instructions_file` is resolved beside the TOML
by the CLI, read once, and sent as text. The server never opens arbitrary
client-supplied instruction paths. The expanded definition and instruction
hash are saved. JSON is the canonical API format.

```bash
entryplug mission instruct <run-id> --file next-turn.md
entryplug mission export <mission-id> > definition.json
entryplug mission update <mission-id> --file definition.json --revision 1
```

An update needs the current revision; an expanded grant or changed body also
requires `--confirm-access-change`. An active turn keeps its pinned revision. Changing bodies closes admissions,
interrupts the old turn, and requires explicit revalidation before new work.
Inputs apply to the next turn and may wake a manual turn. Each definition has
at most one nonterminal run; duplicate starts return that run.

Pause closes new mission admissions and interrupts reasoning. Stop also
requests cancellation. Neither proves a physical stop. `indeterminate` remains
visible and fences additional writers on that body. Recovery never repeats an
uncertain physical action. Restarted watches report a coverage gap and require
source revalidation or an explicit resume. There is no generic “pretend the
effect was safe” reconciliation endpoint.

## Connect a body or external agent

The Connections screen exposes the existing Home Assistant light adapter as
the read-only `homeassistant.light_state` capability. Set a token in the
**service** environment, choose the exact light entity, and use its WebSocket
endpoint (`wss://.../api/websocket` except loopback fixtures). Device reports,
identity, and last-seen data feed the same Session and topology. This does not
turn a light into a camera or detector. Health checks run locally every ten
seconds and report loss/revalidation to missions.

Install only the desired protocol extras:

```bash
python -m pip install 'entryplug[mcp,a2a,homeassistant]'
```

Create an attachment in Connections, selecting a body and exact capabilities.
Save the returned credential in the external client's secret store. Use it as
`Authorization: Bearer <attachment-token>` with:

- MCP: `http://127.0.0.1:8765/mcp/<attachment-id>/`
- A2A Agent Card: `http://127.0.0.1:8765/a2a/<attachment-id>/.well-known/agent-card.json`
- HTTP operations: `/v1/operations`, with `attachment_id` in the request.

MCP uses the pinned official 2.2.0 SDK and its 2026-07-28 protocol. Its tools
require application `runtime_id` and `request_id`. A new transport request may
reuse the same application request ID and gets the same operation. A2A uses
SDK 1.1.5 and protocol 1.0; task artifacts link application operations, native
operations, and evidence. Neither transport owns or closes the body Session.
An external agent controls its own reasoning; Entryplug can revoke its grant
and cancel admitted operations, not stop the external process.

Outbound MCP imports only explicitly named tools advertised as read-only.
Their schemas/catalog are pinned until reconnection. Structured content and
selected images remain evidence, and tool output cannot expand a grant.
Outbound A2A supports one-hop read-only capabilities from an Entryplug Agent
Card; it refuses imported delegation capabilities, pins the card, applies a
20-second deadline, and retains remote task/context/operation identities.
The GUI accepts the MCP endpoint or A2A Agent Card URL, tool names, and a
service-side `ENTRYPLUG_MCP_TOKEN` or `ENTRYPLUG_A2A_TOKEN` reference if credentials are needed.

## Integrate by API

```python
import asyncio
from entryplug_server.client import EntryplugClient

async def main():
    async with EntryplugClient.from_workspace("default") as client:
        mission = await client.create({
            "name": "Entrance watch",
            "instructions": "Alert on validated entry events.",
        }, key="create-entrance-watch")
        run = await client.start(mission["mission_id"], key="start-entrance-watch")
        snapshot = await client.snapshot()
        async for event in client.events(after=snapshot["cursor"], run_id=run["run_id"]):
            print(event)

asyncio.run(main())
```

`GET /v1/openapi.json` and [the checked-in contract](api/openapi.json) describe
the versioned routes. Effectful commands require `Idempotency-Key`; changing
content under the same key conflicts. Mission updates also require `If-Match`.
`202` means accepted: inspect the returned operation/run ID for its outcome.
`id` remains an object identifier; create/start also return `mission_id`/`run_id`.

Snapshot state and its cursor are read through the serialized writer. Subscribe
after that cursor; `Last-Event-ID` is supported by this application's SSE API.
A `410 replay_expired` requires a new snapshot. An open stream can emit `resync`
and close. Durable events have sequence IDs; live text is ephemeral `activity`
without a durable cursor. Streams hold at most 128 durable events per read and
64 live-text items; slow consumers resynchronize. Replaying events never runs
tools. Evidence content is retrieved by authorized ID, not arbitrary file path.

New CLI exit codes are 0 success, 2 validation/missing extra, 3 conflict,
4 unavailable service, and 5 operational failure. Structured errors go to
stderr; JSON results go to stdout. Historical command codes are unchanged.

## Inference and sign-in

Install `entryplug[openai]`. Both routes use the official Responses SDK.

```bash
entryplug auth login chatgpt --profile personal-chatgpt
entryplug auth models --profile personal-chatgpt --json
entryplug auth configure --profile personal-chatgpt --provider chatgpt --model <account-model>
entryplug auth logout --profile personal-chatgpt
# Separate API-key profile; OPENAI_API_KEY must be in the service environment:
entryplug auth configure --profile api --provider openai --model <chosen-model>
```

ChatGPT sign-in follows the official installation registration, PKCE, OIDC,
model discovery, refresh, and revocation flow. Preview requests use an explicit
parameter allowlist, `store=false`, `stream=true`, and local tool namespaces.
Incomplete tool calls never execute. Quota/authentication failures block the
profile, without a retry loop or API-key fallback. Usage is provider-reported
where available; call/time budgets are not an exact billing cap.

Selected observations, instructions, tool results, and supported image bytes
are sent to the configured provider. Local file paths alone do not give the
model vision. Audio/video input, arbitrary compatible endpoints, and managed
Codex/Claude/Hermes/Pi harnesses are not enabled. External MCP/API access works
without provider sign-in. Live ChatGPT account validation has **not** been run;
account eligibility remains a required manual check.

The account-free contract tests consume no credentials. For an explicit live
test, first configure a profile, then run [native_turn.py](../examples/native_turn.py)
with `ENTRYPLUG_LIVE_INFERENCE=1` and `ENTRYPLUG_PROFILE=<profile>`.

## Storage and unattended operation

Named workspaces use XDG config/state/data directories. An explicit directory
uses `config/`, `state/`, and `data/` beneath it. Directories are 0700; private
files are 0600. SQLite uses WAL/FULL and a dedicated serial disk worker.
Tokens have filesystem permission protection, **not encryption at rest**.
Protect workspace backups too: private deduplication responses can contain the
attachment credential returned at creation. Secrets are excluded from public
snapshots, events, exports, HTML, and browser localStorage.

An OS lock rejects a second owner. The resident service binds only 127.0.0.1;
Host/Origin checks, local authentication, HttpOnly SameSite cookies, and CSRF
checks protect browser commands. Use an SSH tunnel for remote access, keeping
the configured loopback authority. Do not expose this single-user service to
the public Internet.

Event retention is 10,000 entries. The default store budget is 256 MiB;
approaching it blocks new work and source ingestion while retaining shutdown,
inspection, and cancellation. Evidence and unresolved operation references
are not silently deleted. Export/backup a stopped workspace before archiving
it; there is no automatic evidence garbage collector in this release.

```bash
entryplug service install --user --workspace default
entryplug service start --workspace default
entryplug service status --workspace default
entryplug service stop --workspace default
```

Installation writes a user unit and reloads systemd; it does not enable it
automatically. The install output names the unit for `systemctl --user enable`.
Logout/reboot survival depends on the user's systemd configuration and linger
(`loginctl enable-linger <user>` where appropriate). Set provider/device
environment variables in a private systemd EnvironmentFile or user-manager
environment. No user unit or linger setting is installed by tests.

## Build and verify

Source builds require Node 22+ and npm; wheel installation and operation do not.
The build hook compiles Solid/Vite and includes assets in the wheel. For a
deliberately offline rebuild of already generated assets, set
`ENTRYPLUG_SKIP_UI_BUILD=1`. `npm ci --prefix ui` uses the committed lockfile.

```bash
python -m pip install -r deps/mission-service.lock
PYTHONPATH=src python scripts/export_contracts.py
npm --prefix ui run types
npm --prefix ui run build
npm --prefix ui test
python -m pytest tests optional_tests/service optional_tests/mcp optional_tests/a2a optional_tests/homeassistant
python -m build --wheel
# Browser tests use an installed Chrome by default; empty selects Playwright Chromium:
ENTRYPLUG_TEST_BROWSER='' npm --prefix ui run test:browser
PYTHONPATH=src python -m entryplug_app.evaluation
```

OpenEnv 0.5.0's FastMCP dependency requires MCP <2, conflicting with the existing
MCP 2.2.0 export. Keep the published pins and run OpenEnv tests in a separate
environment. Its existing finite episodes remain intact; the watch comparison
uses the same application contracts in two finite, isolated workspaces, with
evaluator expectations outside the agent's observations. No unreleased harness
API is imported. See the [delivery report](implementation/mission-delivery.md)
for actual measurements, fixtures, and remaining live checks.
