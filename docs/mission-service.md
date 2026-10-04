# Resident missions

The service owns missions and Sessions. The CLI, browser, Python client, MCP,
and A2A use that owner. Closing an observer leaves the mission running.
The original laboratory commands retain their existing behavior.

## Install and run

Python 3.12+ is required (Python 3.10 is too old). From the project folder,
install the built wheel with console and OpenAI support. If you use Conda:

```bash
# Once, if the environment does not exist:
conda create -n entryplug python=3.12 pip
conda activate entryplug
```

Stop any running Entryplug owner with Ctrl-C before updating. The version is
still 0.0.1, so use `--force-reinstall` to replace an earlier wheel:

```bash
python -m pip install --force-reinstall './dist/entryplug-0.0.1-py3-none-any.whl[ui,openai]'
python -m entryplug ui
```

`entryplug ui --workspace default` opens the console using a one-use local
sign-in. `python -m entryplug ui` also works if the script is not on PATH. If no service exists, `ui` starts one **in the foreground**.
Closing that terminal closes its owner. Ordinary client commands never start a
hidden process. Use `--no-demo` on `serve` to exclude all simulated bodies and their creation.
Use `[ui]` alone if you only want the account-free simulations.

The console has Workspace, New mission, Simulations, Bodies & topology,
Connections, and a persistent inbox. Graph information also has a searchable
keyboard-accessible table. Dragged positions remain stable across updates;
layers and capability groups can be hidden without changing body permissions.

## Explore simulations

Open **Simulations**, select a camera, warehouse rover, or smart room, and
choose **Run with simulated agent**. The service creates a scoped mission and
runs a finite scenario. No account, model call, or hardware is needed.

- **Camera:** a bundled recording plays alongside scripted person events.
  Switch between entry/reentry, disconnection/recovery, and stale observations.
  The agent takes a simulated snapshot and emits alerts for labelled entries.
- **Rover:** the map follows waypoint changes. The agent observes state, calls
  `robot.move`, and stops moving when the simulated obstacle is present.
- **Room:** occupancy changes trigger observation and `room.set_light`; the
  illustrated room reflects the returned light state.

Play, pause, step, reset, and manual event controls belong to the service.
Closing the browser does not stop playback or its mission. Pausing a scenario
stops new source events; **Pause** or **Stop mission** controls the agent run.
Playback is finite and does not loop automatically. Custom body definitions
survive a restart; their simulated world resets and playback remains paused.

The **simulated agent** is a deterministic demonstration policy using the same
scoped tools, operation journal, and evidence flow as missions. It cannot use
connected hardware. The **local rule** is real automation without an LLM; it
can process validated person-entry capabilities on either simulated or connected
bodies. Body labels identify which source is in use. The **Entryplug agent**
uses a configured live model profile and may incur provider usage.

The camera footage is [Big City Life, Coverr (2015)](https://commons.wikimedia.org/wiki/File:Big_City_Life.webm),
distributed under CC0. The unmodified recording is bundled for offline use.
Scripted events are independent of its pixels: no person detector or video
analysis is performed. Camera evidence identifies this recording as illustrative.

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
detector**; the UI recording is illustrative. A watch returns to waiting after each bounded turn.
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

Use **Add body** from Bodies & topology or the mission wizard. The icon picker
offers simulations, Home Assistant, MCP, A2A, ROS 2, Zenoh, and MQTT. A simulated
camera, rover, or room can be named and added immediately. Connected bodies
show their connection status and verified capabilities after configuration.

ROS 2, Zenoh, and MQTT selections currently require a user-provided **MCP bridge**
that exports the desired read-only tools. The protocol label records the declared
source behind that bridge. The console does not perform direct native discovery
or create a bridge. Selecting an icon alone does not establish a connection.

Saved connections show their observed status and last-seen time. **Retry
connection** starts a new verification attempt for a disconnected or unverified
body. If the service environment gains a new credential, restart Entryplug
first so the process receives it. **Test read-only** checks a connected body's existing health endpoint
without invoking a capability or granting writes. A failed probe is reported
separately; the resident health loop updates mission health and body status.
Expand **Used by missions** before disconnecting to see each dependent mission
and its current run status. Retry and test are also available at
`POST /v1/connections/{connection-id}/retry` and `/test` for owner clients.

The Home Assistant option exposes the existing light adapter as
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

Install the wheel with `[ui,openai]` as shown above. Select **Entryplug agent**
in New mission or open Connections to configure a model. The agent choice is
available before sign-in; missing login libraries display an installation command
and restart instructions. ChatGPT sign-in preserves the mission draft in this
browser tab. After sign-in, load an account model and save the profile before
continuing. API-key profiles separately read `OPENAI_API_KEY` from the environment
of the process that starts the service. Both routes use the official Responses SDK.

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
python -m pip install -r deps/dev-tools.txt -r deps/test-tools.txt -r deps/build-tools.txt
python -m pip install --require-hashes -r deps/mission-service.lock
python -m pip check
npm ci --prefix ui
PYTHONPATH=src python scripts/export_contracts.py
npm --prefix ui run types
npm --prefix ui run build
npm --prefix ui test
python -m pytest tests optional_tests/service optional_tests/mcp optional_tests/a2a optional_tests/homeassistant
python -m build --wheel --no-isolation
python scripts/check_wheel_assets.py dist/entryplug-0.0.1-py3-none-any.whl
python -m pip install --no-deps --force-reinstall dist/entryplug-0.0.1-py3-none-any.whl
# Browser tests use an installed Chrome by default; empty selects Playwright Chromium:
(cd ui && npx playwright install --with-deps chromium)
ENTRYPLUG_TEST_INSTALLED=1 ENTRYPLUG_TEST_BROWSER='' npm --prefix ui run test:browser
PYTHONPATH=src python -m entryplug_app.evaluation
```

Install tooling and the runtime lock in separate pip commands: hashes in the
runtime lock enable pip's hash-required mode for that entire invocation. Keep
runtime hash verification enabled. Use a fresh Python 3.12 virtual environment
for core and a separate one for service; inherited optional packages or
`PYTHONPATH` can mask failures that appear on CI.

`ENTRYPLUG_TEST_INSTALLED=1` makes the browser server and CLI parity check use
Python isolated mode from the temporary directory. They import the installed
wheel, including its console assets, without source-path injection. Omit that
flag for ordinary source development. This validates the labelled simulation
product journey; it does not package the native container scenario fixtures.

OpenEnv 0.5.0's FastMCP dependency requires MCP <2, conflicting with the existing
MCP 2.2.0 export. Keep the published pins and run OpenEnv tests in a separate
environment. Its existing finite episodes remain intact; the watch comparison
uses the same application contracts in two finite, isolated workspaces, with
evaluator expectations outside the agent's observations. No unreleased harness
API is imported. See the [delivery report](implementation/mission-delivery.md)
for actual measurements, fixtures, and remaining live checks.

## Native inspection missions (development)

The `inspection` driver executes one `inspect_target` operation without a model.
It reads the configured target from that capability's schema, uses ordinary
mission permissions and records the native operation and its evidence. Failed,
refused or uncertain inspection blocks the mission; it does not claim an absent
target or a successful observation. Free-text instructions are recorded and
versioned but do not program this deterministic driver.

For a service already supplied with an inspection `EmbodimentPort`, generate
and create the default approval-required mission:

```sh
entryplug mission template --body borrowed-light --json > inspection.json
entryplug mission validate inspection.json
entryplug mission create inspection.json --workspace YOUR_WORKSPACE
```

Start the returned mission ID using `entryplug mission start ID`; review the
operation approval and evidence in the existing console. The body must expose
`inspect_target` with a fixed `target_id`; a Home Assistant light-state connection
alone does not provide that task or grant lighting authority.

The owned Borrowed Light development fixture can now run both first and warm
inspections through the resident application owner:

```sh
python3 containers/scenarios/run_hybrid.py --build --client mission
```

This provisions disposable HA/MQTT and ROS/MuJoCo/native Zenoh services using the
existing runner. Only this owned fixture grants inspection without per-operation
approval. Its report joins mission runs, application operations, native results
and durable evidence. Camera/light devices remain simulated; middleware is native.
No model account or model calls are required. The mission route also accepts `--fault ha-result-loss`: a fixture-only relay
withholds a real HA service response until public ROS feedback confirms the
command applied, then drops the connection. The evaluator requires an unknown
effect, a blocked mission, stable duplicate-request IDs and refusal of further
writes. The normal adapter and task do not receive evaluator state.

This is checkout-based development qualification. Native fixture packaging,
turnkey `serve` attachment, native browser journeys, mission fault qualification
and installed native fixture bundles remain open. The service's existing Python
composition seam is `ApplicationService(..., ports=[EmbodimentPort(..., session)])`;
use the same task Session rather than constructing a second operation authority.
