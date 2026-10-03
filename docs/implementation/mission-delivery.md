# Mission service delivery

Implemented against clean baseline `840fe959a07e8f2a95276f738228ff123d6d48c7`
on 30 September 2026. The interface specification superseded the previous
distributed-embodiment work order for this change. The original planning files
and physical-runtime architecture were left intact. See the [initial audit](mission-runtime-audit.md)
and [operating guide](../mission-service.md). Runtime/API commit: `211d43e`;
console/packaging commit: `f4099a6`. Measurements were made against those
implementation contents before the accompanying documentation commit.

## Delivered behavior

The resident application owns Sessions and durable mission state. CLI, browser,
HTTP/Python, MCP and A2A clients share its admission and operation journal.
Closing an observer leaves its mission running. Pause, cancellation, source
loss and process recovery preserve uncertain physical outcomes.

Existing `Session`, `OperationHost`, capability admission, operation/effect states,
evidence, protocol exports and the Home Assistant light adapter were reused.
Added modules cover revisioned definitions, SQLite persistence and event replay,
bounded turns and triggers, scoped attachments, approvals, persistent alerts,
native inference, provider profiles, the local HTTP boundary and Solid console.
The core remains usable without the service, UI or provider extras.

The free watch uses an explicitly **SIMULATED**, labelled source. Its events
distinguish sustained presence, separate people, reentry and detector epochs.
Home Assistant exposes actual adapter readback through a read-only capability;
its end-to-end check used a local WebSocket device fixture. Neither is a claim
to have implemented or tested a real camera person detector.

The console includes overview/embark, mission operation, body/topology,
connections and a persistent inbox. It uses the generated API contracts and
provides a keyboard-accessible topology table alongside the optional graph.
Native OpenAI and ChatGPT profiles share a bounded tool broker. Protocol clients
borrow the owner instead of creating another physical Session.

ChatGPT registration, PKCE/OIDC, model discovery and request filtering follow
the official [sign-in contract](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
and [models/inference contract](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).
Tests use the official SDKs and local HTTP fixtures. **Live account sign-in and
inference are not validated.** Eligibility and current preview behavior require
the documented opt-in check; they are not marked as passed.

## Versions and reproducibility

Runtime: CPython 3.12.14, Linux 6.8.0-138 x86_64, glibc 2.35.
The [service lock](../../deps/mission-service.lock) records resolved dependencies
and hashes; [compatibility.json](../../deps/compatibility.json) separates these
checks from historical substrate qualification.

| Component | Tested version |
| --- | --- |
| Starlette / Uvicorn / sse-starlette | 1.7.0 / 0.54.0 / 3.4.11 |
| Pydantic / HTTPX | 2.13.5 / 0.28.1 |
| OpenAI / Authlib | 3.22.1 / 1.8.0 |
| MCP / httpx2 | 2.2.0 / 2.13.1 |
| A2A SDK / Protobuf / aiohttp | 1.1.5 / 7.36.2 / 3.14.3 |
| OpenEnv, in a separate environment | 0.5.0 |
| Node / Solid / Cytoscape | 22.23.3 / 1.9.15 / 3.34.3 |
| TypeScript / Vite / vite-plugin-solid | 5.9.3 / 8.3.1 / 2.11.14 |
| openapi-typescript / Vitest / Playwright | 7.13.0 / 5.0.2 / 1.63.0 |
| Chrome | 154.0.8037.92 |
| pytest / pytest-asyncio / Ruff / mypy | 9.1.1 / 1.4.0 / 0.13.3 / 1.18.2 |
| build / setuptools / wheel | 1.6.1 / 84.0.0 / 0.48.0 |

Verification used isolated interpreters under `/tmp`; no host service, system
Python, provider account or physical device was configured. Commands below use
`python` for the corresponding isolated interpreter and Node 22 on `PATH`:

```bash
PYTHONPATH=src python -m pytest -q tests optional_tests/service optional_tests/mcp optional_tests/a2a optional_tests/homeassistant
# Separate OpenEnv environment:
python -m pytest -q optional_tests/openenv
ruff check src tests/app scripts optional_tests/service optional_tests/homeassistant/test_service_connection.py examples/native_turn.py setup.py
PYTHONPATH=src mypy --follow-untyped-imports -p entryplug_app -p entryplug_server -p entryplug_agents
PYTHONPATH=src python scripts/export_contracts.py
npm --prefix ui run types
npm --prefix ui run build
npm --prefix ui test
ENTRYPLUG_TEST_PYTHON=/path/to/python npm --prefix ui run test:browser
python -m build --wheel --no-isolation
python scripts/check_wheel_assets.py dist/entryplug-0.0.1-py3-none-any.whl
PYTHONPATH=src python -m entryplug_app.evaluation
```

Generated JSON and TypeScript were regenerated and their hashes compared:
no contract drift. Ordinary tests made no real model calls.

## Executed checks

| Check | Result and evidence |
| --- | --- |
| Python core, application, HTTP, providers, MCP, A2A and Home Assistant | **344 passed**, one Authlib `jose` deprecation warning; [log](evidence/python-tests.txt) |
| Existing OpenEnv regressions | **3 passed** in a separate environment; [log](evidence/openenv-tests.txt) |
| New application/server/provider strict typing | **27 source files passed** |
| Ruff | Passed for the command above |
| UI unit tests | **3 passed**: auth/CSRF, revision headers and replay recovery |
| Browser workflows | **2 passed**: graph paint and center selection, GUI launch, CLI pause after browser closure, mobile inbox/keyboard acknowledgement; latency and graph workload |
| Built wheel | Installed and served outside the repository with **Node absent**; authenticated health, compiled assets, SIGTERM owner cleanup and durable `service.stopped` verified; [result](evidence/wheel-smoke.json) |
| Core-only installation | CLI imports/parser/help worked without service/provider/protocol imports |

Whole-repository default `mypy` is **not green in this environment**: there are
13 errors in six files involving optional OpenEnv, NumPy and MQTT typing/imports.
Running the original commit in a separate archive produces the same 13 errors.
The [baseline log](evidence/baseline-mypy.txt) and [current log](evidence/repository-mypy.txt)
are retained; this is distinct from the passing checks for the 27 new modules.

Failure and recovery coverage is executable in:

- [Process recovery](../../tests/app/test_process_recovery.py): real child-process
  death before dispatch, during sending and after a possible effect; no replay.
- [Mission failures](../../tests/app/test_failures.py): bounded queues, source
  loss, uncertain operations, revision-bound approvals, pause invalidation and
  ten minutes of simulated event-driven inactivity with zero model calls.
- [Event limits](../../tests/app/test_event_limits.py): concurrent snapshot/cursor,
  replay retention, slow subscribers and preserved evidence references.
- [Protocol fixtures](../../optional_tests/service/test_protocols.py): HTTP/MCP
  application deduplication across reconnect, Session ownership, scope revocation,
  linked A2A artifacts and cancellation with an unconfirmed physical result.
- [Outbound fixtures](../../optional_tests/service/test_outbound.py): structured
  MCP results and separate A2A task/context/operation/evidence identities.
- [Provider streams](../../optional_tests/service/test_provider_stream.py) and
  [authentication](../../optional_tests/service/test_auth.py): actual SDK streams,
  completion versus disconnect/quota, atomic refresh, logout races and profile
  blocking. Partial or out-of-schema tools do not execute.
- [Home Assistant](../../optional_tests/homeassistant/test_service_connection.py):
  actual adapter readback against a WebSocket fixture, loss and revalidation.

The [finite watch comparison](evidence/watch-evaluation.json) includes the same
12 labelled steps and retained evidence for both policies: four alerts with
zero rule-model calls and four **simulated** scripted calls. This is a repeatable
contract comparison, not a result showing that an LLM improves detection.

## Measured resources

Reference hardware: Intel Core i5-12500T, 12 logical CPUs; browser viewport
1440 × 1000. No model process, video worker or physical-network latency is
included. SDK memory is measured in the same Python process.

| Workload | Measurement |
| --- | --- |
| Initial JavaScript, graph deferred | 19,856 bytes gzip (19.4 KiB) |
| Deferred graph JavaScript | 137,213 bytes gzip (134.0 KiB) |
| CSS | 4,442 bytes gzip |
| 30 alternating pause/resume commits to visible DOM | p95 87 ms, loopback |
| 1,000 nodes / 2,000 edges; 10-node batches at 5 Hz; pointer pan for 10 seconds | 57.7 animation frames/s, 50 batches, no relayout |
| Five authenticated browser reloads against the running owner | p95 60 ms; includes loading and snapshot, excludes a service restart |
| Foreground owner startup to authenticated HTTP | 233 ms |
| 60 seconds HTTP idle, one SSE observer | 0.55 CPU seconds, 0.92% of one CPU; 37.3 MiB RSS; zero model calls |
| Core owner idle without HTTP observer, 60 seconds | 0.000805 CPU seconds; zero model calls |
| Ten-minute watch, 600 source steps at 1 Hz | 200 turns / 200 alerts; zero model calls |
| Ten-minute watch memory, with all measured SDKs imported | 95.7 → 99.2 MiB sampled RSS; 116.6 MiB peak |
| Watch store | 4.09 MiB after close; approximately 7.7 MB with WAL during the run |

Bundle sizes use Python `gzip.compress(..., mtime=0)`, compression level 9;
Vite's displayed gzip totals use different compression settings. Raw values
are in [bundle sizes](evidence/bundle-sizes.json), [browser metrics](evidence/browser-metrics.json),
[HTTP idle](evidence/http-idle.json) and [resource measurements](evidence/resources.json).
Reproduce the last two with `scripts/measure_http_idle.py` and
`scripts/measure_missions.py --seconds 600 --output result.json` using `PYTHONPATH=src`.

Staged RSS in KiB was core 24,196; server 30,896; OpenAI 61,248; Authlib 71,428;
MCP 89,036; A2A client 96,540; Home Assistant 96,732. These are cumulative
imports, not independent per-extra costs. The graph number measures a fixed
Cytoscape interaction fixture using the production rendering options. It counts
animation-frame callbacks, not GPU-presented frames; cached viewport drawing
helps panning. It does not establish the same rate for arbitrary full redraws.
No performance-saving claim is made against the old runtime.

The [rounded-polygon graph run](evidence/browser-rounded-polygon-metrics.json)
was retained: 44.93 frames/s, below the 45 target. The delivered graph uses
circular nodes, measured above, and its browser test selects the exact node
center. Plain diamonds exposed an SDK polygon hit-test edge case at that point.
The measured runs describe this fixture and machine, not a general speedup.

Incremental wheel rebuilding initially retained obsolete chunks. The build hook
now clears its generated asset destination, and the wheel-asset check above
compares the archive with the current compiled source. The final wheel contains
exactly five console files, including runtime dependency license notices.

[Desktop mission screenshot](evidence/mission-desktop.png) ·
[Mobile inbox screenshot](evidence/inbox-mobile.png).

## Active limits and unexecuted checks

- Live ChatGPT login, quota behavior with an eligible account, paid API inference,
  physical Home Assistant devices and ROS/container substrate tests were not run.
  The explicit opt-in [native example](../../examples/native_turn.py) is provided.
- The source is labelled simulation; no actual video capture, tracking model or
  person-detector coverage is claimed. Interval image sampling has visible gaps.
- Topology covers configured bodies/capabilities and the observed adapter path.
  It is not universal middleware discovery. Outbound MCP is explicitly read-only;
  outbound A2A is one hop with a pinned Entryplug capability card.
- OpenEnv 0.5.0 pulls FastMCP with MCP <2, conflicting with the retained MCP 2.2.0
  pin. Separate environments are required. No unreleased OpenEnv harness wrapper
  or managed Codex/Claude/Hermes/Pi driver is claimed.
- Evidence is retained rather than silently collected. The 256 MiB store budget
  blocks new work; archiving/garbage collection and generic physical uncertainty
  reconciliation remain explicit future work. The soak was ten minutes, not days.
- The user systemd unit generator is implemented; no unit or linger configuration
  was installed on this host. The CI workflow was added, not run remotely.
- Screenshots, measurements and logs stay in the repository. An optional CI
  artifact-upload step was rejected by automatic approval review and omitted.

## Simulation workbench update — 30 September 2026

Implementation commit: `cf7480f`.

The follow-up replaces promotional screen headings with task names, increases
control/body text, and adds camera, rover, and room simulations with their own
state. A deterministic simulated agent uses journalled observations and actions;
its UI label distinguishes it from both the local rule and live model inference.
Playback belongs to the service. Custom body definitions persist, with playback
paused and simulated state reset after restart. The original evaluation-only
scripted driver remains separate.

The camera view includes an unmodified, offline CC0 recording with attribution
and an explicit statement that scripted events are independent of its pixels.
Rover movement/obstacle and room occupancy/light changes appear in illustrated
views. The body picker uses labelled protocol glyphs; ROS 2, Zenoh and MQTT
require a configured read-only MCP bridge, not direct native discovery.

The mission wizard no longer disables native agent setup before a profile exists.
Missing OpenAI libraries produce an actionable installation message in both API
and UI. Account sign-in preserves the mission draft; model selection completes
the profile. Browser tests verify the sign-in request using a fixture; **live
ChatGPT account sign-in remains unverified**. No credentials or paid calls were
used. Upgrade/restart instructions are in the [operating guide](../mission-service.md).

Validation used the same isolated Python/Node/SDK versions listed above:

- **350 Python tests passed**, one existing Authlib deprecation warning;
  [log](evidence/workbench-python-tests.txt). Command: the Python regression
  command above, including service, MCP, A2A and Home Assistant optional tests.
- **28 application/server/provider modules passed strict mypy**, and the Ruff
  command above passed. Unchanged isolated OpenEnv tests and whole-repository
  optional-dependency typing were not rerun for this update.
- **3 UI unit tests and 6 browser tests passed**; [browser log](evidence/workbench-browser-tests.txt).
  Coverage includes actual video playback/pause, rover motion and obstacles,
  body creation, room light state, mobile layout, protocol bridge copy, and login
  dependency handling alongside the existing lifecycle and topology workflows.
- Regenerated JSON/TypeScript contracts and built the console and wheel.
  `check_wheel_assets.py` found exactly seven current console files, without
  stale chunks. The installed wheel served all three simulations and HTTP range
  video outside the repository with Node absent; SIGTERM released the owner and
  journalled `service.stopped`. [Wheel result](evidence/workbench-wheel-smoke.json).
- Initial JavaScript is **26,849 bytes gzip**, CSS **6,786 bytes gzip**, and the
  deferred graph is **137,213 bytes gzip**. The offline recording adds **4,225,837
  bytes**. [Exact sizes](evidence/workbench-bundle-sizes.json).
- The existing browser workload measured **106 ms** p95 state visibility,
  **187 ms** p95 reload/reconnect, and **40.2 frames/s** for the separate
  1,000-node/2,000-edge graph fixture. [Raw samples](evidence/workbench-browser-metrics.json).
  These are current observations, not a claim of performance improvement; idle
  CPU and long-duration resource measurements were not repeated.

Visual evidence: [camera](evidence/workbench-camera.png),
[rover](evidence/workbench-rover.png), [room](evidence/workbench-room.png),
[mobile](evidence/workbench-mobile.png), [body connections](evidence/workbench-add-body.png),
and [provider setup](evidence/workbench-agent-setup.png). All evidence is local.

## Connection diagnostics — 2 October 2026

Implementation commit: `6962b0d`.

Connections now offers a read-only live health check for connected Home
Assistant, MCP, and A2A bodies. Failed or explicitly disconnected bodies can
be retried without creating duplicate connection tasks. Disconnect closes the
previous health loop before a later retry, and a late connection failure cannot
replace an intentional disconnected state. The screen shows last-seen state and
the mission definitions and active runs that depend on a body. No device command
is sent by Test read-only; an unavailable result is displayed separately while
the resident health loop updates mission health. New service environment values
require a service restart before Retry can use them.

The owner-only routes are `POST /v1/connections/{id}/test` and `/retry`. The
first is a read-only probe; the second is an idempotent owner command. The
console build identifier advanced to `mission-workbench-v3` so old service
processes reject the new controls until restarted.

Validation: the Home Assistant WebSocket fixture reproduced missing credentials,
then proved retry, live read-only health, an unavailable report, disconnect,
and immediate reconnect without device commands. The full Python regression
suite passed **351 tests** with one existing Authlib deprecation warning;
[log](evidence/connection-python-tests.txt). Strict mypy passed for 28 application,
server, and provider source files; Ruff passed. The console build, three UI
unit tests, and [seven browser workflows](evidence/connection-browser-tests.txt)
passed, including a failed body and its dependent mission on the Connections
screen. The [screenshot](evidence/connection-diagnostics.png) records this view.

The repeated browser workload measured **92 ms** p95 state visibility,
**89 ms** p95 reload/reconnect, and **58.9 frames/s** for the fixed 1,000-node,
2,000-edge graph fixture; [raw samples](evidence/connection-browser-metrics.json).
An earlier concurrent test run measured 40.2 frames/s, so the result depends on
host load. The [wheel smoke test](evidence/connection-wheel-smoke.json) installed
outside the repository with Node absent and verified the console assets,
three simulations, video range serving, health, and owner cleanup.

## Distributed inspection qualification — 3 October 2026

Implementation commits: `b525516` (test environments), `9ff7b87` (freshness),
`fe861ba` (native scenarios and evidence).

`inspect_target` now checks frame age after compute and checks both verifying
frames together at acceptance. Stale output cannot establish success or trigger
another light adjustment. Cancellation/deadline checks surround pending capture,
compute and alternate warmup; known lighting effects remain reported on failure.
Results include the local acceptance clock, sample ages and quality, with native
worker generation, job identity and input digest when that adapter supplies them.
The timing profile remains 500 ms; the 10 ms/60 ms regression uses a controlled
clock and does not redefine production timing.

The hybrid evaluator now requires distinct progressing samples, valid quality,
source/program identity, matching raw-frame hashes and native input digests,
post-application simulation timestamps and native worker shutdown. Mutation tests
reject empty, stale, malformed and contradictory evidence. Freshness and repair
checks are separate from the missing-worker-at-startup refusal.

Two scenario selectors now launch actual rendered cases:

```bash
./entryplug test --suite scenarios --scenario hybrid --client session --build
./entryplug test --suite scenarios --scenario hybrid --client session --fault no-native-worker
./entryplug test --suite scenarios --scenario zenoh --build
./entryplug test --suite scenarios --scenario hybrid --client session --fault kill-active-worker --build
```

An unchanged no-build run verifies image/source identity and performs no image
build or pull. Changed source requires `--build`; source changes during image
preparation also cause refusal. `mqtt`, `ros2-vision` and `ros2-recovery` remain
explicitly unsupported selectors. Aggregate/acceptance/offline selectors are
still future work. These are source-checkout commands, not installed demo claims.
The hybrid `direct` client remains an invocation mode using OperationHost, not
the no-OperationHost performance baseline.

The standalone Zenoh case starts its Session before the detector process joins.
It records an unavailable operation, performs an explicit idle rebind after the
supervisor starts the approved worker, then obtains two successful requests with
new samples. A later request with a mismatched worker generation is refused.
The same Session/runtime survives all four operations. Fixture setup applies
one adequate light setting before admission; the read-only tasks make zero
lighting writes. This lane runs the ROS camera, MuJoCo and a separate native
router/worker, with no HA or MQTT process.

The hybrid active-loss case holds the primary's third validated job **before its
native reply**, then SIGKILLs that owned process. The held result, input sample
and actual process exit are retained. This qualifies active request/result loss,
not arbitrary network packet loss or a kill during detector arithmetic. The
ordinary task uses one approved alternate, records binding revision 2 and stays
within its original 30-second deadline. The warm operation also uses worker-b
with zero light writes. The current task-local policy retries the poisoned
primary and rechecks the alternate on the next operation; no persistent worker
selection efficiency is claimed.

[Retained manifests and operation evidence](evidence/distributed-inspection.json)
record source digests, image IDs, operation IDs, sample ages, shutdown outcomes,
exact commands and unsuccessful build attempts. The full raw frame/log directories
remain local under `runs/` and are not an installed or public evidence bundle.
The no-fault hybrid run recorded writes `[3, 0]`; its verifying frame ages were
approximately `[178, 14]` ms and `[192, 17]` ms. These are development observations,
not a speed or reliability claim. Later lane changes have their own source hashes.

Validation used CPython **3.12.3** in the cached Jazzy substrate, pytest **9.1.1**,
pytest-asyncio **1.4.0**, Ruff **0.13.3**, and mypy **1.18.2**. The initial portable
baseline passed 317 tests. The final portable/service/MCP/A2A/HA/Zenoh/MQTT run
passed **405 tests**, with one explicit broker-port test skipped and the existing
Authlib deprecation warning; [test/type log](evidence/distributed-python-tests.txt).
An earlier expanded run failed because the temporary environment lacked HTTPX;
installing the existing locked service profile resolved that dependency gap. The service extras came from `mission-service.lock`.
Strict mypy passed for the inspection task and native detector adapter; touched
Python Ruff, shell syntax, Compose configuration and diff whitespace checks passed.
The final source-labelled Harbor/mixed/worker builds and the four live runs above
passed. Browser, wheel, OpenEnv, old arm release and held-out suites were not rerun.

`deps/test-tools.txt` now pins async-capable portable/service test tools in CI
and in an isolated Harbor test venv. ROS launch tests retain Jazzy's distro pytest
7.4.4: its `launch_testing` plugin is incompatible with pytest 9. Two failed builds
are retained: attempting to replace Debian-owned pluggy, then loading that ROS
plugin under pytest 9. Neither is counted as a pass.

The mission service/console behavior was preserved. Connecting this native body
to a resident inspection mission, the remaining scenario lanes and native faults,
installed fixture packaging, exported native-task qualification, strong comparisons
and freeze/release gates remain open. No hardware, second-host, model-token or
live account claim follows from these development runs.

## MQTT inspection lifecycle — 3 October 2026

Implementation commit: `447d290`. Three scenario selectors now have live
qualification: `hybrid`, `zenoh`, and `mqtt`. The two ROS selectors remain
explicitly unsupported; aggregate, acceptance and offline modes remain open.

```bash
./entryplug test --suite scenarios --scenario mqtt --build
./entryplug test --suite scenarios --scenario mqtt
```

The MQTT lane uses the same `inspect_target`, OperationHost and Session. It starts
with no bridge and a retained bright MQTT state: inspection refuses with zero
writes. After the bridge joins, the same Session succeeds using three absolute
light commands, MQTT readbacks of applied ROS revisions, and checked rendered
frames. A warm request succeeds with fresh frames and zero writes. Following a
graceful bridge stop, retained bright state cannot make another request succeed.
The four outcomes are failed/succeeded/succeeded/failed; writes are `[0, 3, 0, 0]`.

The fixture control port requires a new nonce-bound, non-retained response from
the bridge. The world owner answers only with a recently received ROS state.
This is a fixture protocol extension; standard HA JSON-light commands and states
remain compatible. Physical commands are bounded absolute settings, sent once
at QoS 0 without retention; read-only challenges may repeat. Unconfirmed writes
remain uncertain under the existing task policy. This is a single-writer profile,
not an exactly-once or atomic shared-control guarantee.

Compute is explicitly the approved **local** detector. No HA or native detector
process starts in this lane, and its selected image preparation excludes the
native worker image. The camera has no bound direct ROS lamp publisher; task
writes use MQTT. Readback remains device-reported evidence; the acceptance check
separately requires fresh rendered frames after the application boundary.

[Structured evidence](evidence/mqtt-inspection.json) includes both MQTT runs and
a subsequent hybrid compatibility run, actual image/source identities, native
MQTT readbacks and commands, operation results, camera hashes and cleanup. Raw
PNGs/logs remain in `runs/mqtt-5243f852d871/`, `runs/mqtt-f83a5f46b976/`, and
`runs/hybrid-578232e28991/`. The unchanged MQTT rerun performed no build or pull.
The hybrid run retained two fresh HA enrollments, native worker exit 0 and writes
`[3, 0]`. Every owned fixture was removed. These are development cases; bridge
loss during a write and interrupted startup are still unqualified.

Validation: **432 passed, 1 skipped**, with the existing Authlib warning, using
CPython 3.12.3, pytest 9.1.1 and pytest-asyncio 1.4.0; [log](evidence/mqtt-python-tests.txt).
The skip is the optional standalone broker test without an explicit port, not the
live MQTT lane. Strict mypy 1.18.2 passed for both fixture MQTT modules; Ruff
0.13.3, shell syntax, Compose configuration and diff whitespace passed. Mutation
tests refuse missing raw frames/readbacks, reused nonces, retained/replayed
commands, false readiness, fabricated native-worker identity and forced cleanup.
No browser, wheel/bundle, OpenEnv, old arm-release or held-out suite was rerun.

Remaining work is in the existing plan: ROS scenario wrappers and remaining
faults, then the real Borrowed Light resident mission, installed/exported task
qualification and separate freeze/comparison/release gates. This slice does not
complete Slice B, Slice C or M3 and adds no physical-hardware or live-account claim.

## ROS scenario lanes and startup ownership — 3 October 2026

Implementation commits: `5d97cac` (recovery selector), `c2c1fa5` (continuing
vision Session and startup cancellation), `77846ce` (record isolation and live
startup interruption). All five scenario names now dispatch implemented runners.
This does not close the aggregate, installed acceptance or release gates.

```bash
./entryplug test --suite scenarios --scenario ros2-vision --build
./entryplug test --suite scenarios --scenario ros2-recovery
```

The ROS vision lane creates its Session before the world exists. Inspection
reports unavailable and admission refuses without dispatch. The existing idle
reconfiguration barrier stays closed while the actual ROS camera/controller,
local detector workers and acquired visual binding become ready. The same
Session/runtime then completes two distinct image-row goals using new frames.
Removing the idle owned world causes a failed, confirmed-stopped operation and
fences another admission. Raw image hashes and independent visual scoring are
required by the evaluator. A separate existing Hall of Mirrors development world
selects the controlled camera and rejects stale and unrelated sources. This
remains local one-dimensional alignment, not 3D reach or hardware qualification.

The recovery selector runs eight fixed development cases sequentially: ordinary
reuse, active-worker repair, stale and absent replacement, private task-reply
loss, native-client exit after goal acceptance, cancellation during a native
replacement probe, and loss of public joint feedback. It uses existing resident
evaluators and the unchanged numerical profile. Repair evidence explicitly joins
the admitted operation, target and deadline to the completed task. No holdout seed
is selectable through this runner. Required case failures make the suite fail.

The first recovery attempt retained **7/8 passes and overall failure**: measured
image noise caused a legitimate cached-binding refusal. The second likewise
retained **7/8 passes and overall failure**: one world failed before ready on a
non-JSON private record. Its exact bytes were not retained by the old parser, so
the producer of that original line is unknown. No thresholds, probe sizes or
noise requirements were changed to erase either failure.

A reproduced startup cancellation bug is fixed: cancellation during the ready
handshake now stops the already-started owned world and propagates cancellation;
an unconfirmed stop remains an explicit startup failure. Python and native fd-1
diagnostics now go to stderr, while JSON records use a dedicated descriptor.
Malformed records are refused and saved locally with a size bound and mode 0600.
A real interrupted-start test passed after the controller report appeared and
before readiness completed; cancellation propagated and the world was removed.

Final live verdicts: **ROS recovery 8/8 passed (passed)** and **ROS vision passed**, including
startup interruption, continuing goals, resource-loss refusal and Hall of Mirrors.
The final recovery command reused the same source-labelled Harbor image without
building or pulling. The ROS lanes use only the Harbor image and its network-none
profile. [All manifests and selected native receipts](evidence/ros-scenarios.json)
retain failed attempts, source/image/profile identities and evidence paths. Raw
images and full traces remain in ignored `runs/`; this is not an installed or
complete public fixture bundle.

Validation: **473 passed, 1 skipped**, with the existing Authlib warning;
[log](evidence/ros-python-tests.txt). Tests and native fixtures used Python 3.12.3,
pytest 9.1.1 and pytest-asyncio 1.4.0; the source-checkout host launcher used Python
3.14.4. Strict mypy 1.18.2 passed for resident lifecycle and record isolation; Ruff
0.13.3 and diff checks passed. The skipped standalone MQTT test lacked an explicit
broker port. Browser, wheel/bundle, OpenEnv and held-out release suites were not
rerun. The portable cancellation and malformed-record failures before the fixes
remain retained separately.

The authority audit still distinguishes portable worker-generation, competing
client and hung-agent tests from live resident proofs. Independently lost native
admission acknowledgement, aggregate/offline modes, remaining mixed faults, native
Borrowed Light missions, installed/exported use and separate freeze/comparison
work remain open. Current native-result loss proves client exit **after** native
acceptance; it does not establish network packet loss or admission ambiguity.

## Aggregate/offline scenarios and interruption fixes (3 October 2026)

Commits `6f51210`, `fa8e3a2`, and `885c5d8` add the sequential five-lane
source-checkout suite, offline preflight and retained per-lane results; preserve
owned cleanup and primary failure diagnostics; and inhibit further HA writes
when cancellation or deadline expiry interrupts an uncertain native command.
See the [scenario operating guide](../scenarios.md).

```sh
./entryplug test --suite scenarios --build --json
./entryplug test --suite scenarios --offline --json
./entryplug test --suite scenarios --tier acceptance --offline --json
```

The first build command returned **blocked** before launching fixtures: the native
worker image build failed. The old helper discarded its detailed output, so its
cause remains unknown. A diagnostic rebuild of that image at the identical source
succeeded. Failed future builds retain a private mode-0600 diagnostic in a
mode-0700 local directory; dependency output is not printed or published.

The subsequent offline aggregate, `runs/scenarios-32c03d061373/`, returned
**failed, 4/5 lanes passed** at source commit `6f51210`. Zenoh, MQTT, hybrid and
ROS recovery passed; recovery passed all eight cases. ROS vision passed actual
startup interruption and Hall of Mirrors but refused its first connected visual
goal: measured noise was 1.913305 px and reuse signal-to-noise was 1.879631,
below the unchanged minimum 2. The existing safe refusal remains a failure.
No threshold changes or retries seeking a pass were made. All normal-run owned
containers, networks and volumes were removed. Sequential lane execution took
981.45 seconds; this is elapsed development work, not a performance comparison.

The original vision summary misleadingly overwrote this failure with “runtime
restarted,” although the retained runtime IDs match. A regression now preserves
the primary execution failure and records subsequent validation failure separately.
A disposable-process regression also showed that the CLI's subprocess helper
killed the runner before Ctrl-C cleanup finished. The CLI now forwards SIGINT to
its owned runner and waits for cleanup. Whole native-suite interruption remains
separate from this process-level regression and the already-qualified interrupted
ROS startup case.

Four HA protocol regressions reproduced a cancellation/deadline gap at service
response and state-readback boundaries: another brightness write was allowed
without explicit reconciliation. Cancellation now marks the native effect
uncertain, drops the connection and propagates cancellation. Further writes are
inhibited until explicit reconnect/reconciliation; nothing is replayed. A fifth
control confirms cancellation before dispatch does not incorrectly inhibit an
unsent effect. These tests use a local WebSocket server, not upstream HA fault
qualification.

Final validation: **509 passed, 1 skipped**, with the existing Authlib warning;
[full test log](evidence/scenario-suite-python-tests.txt). Strict touched-file
mypy and Ruff passed. The one skip remains the standalone MQTT test without an
explicit broker port. An intermediate run exposed outdated CLI mocks and a
pre-existing HTTP test race between alert creation and turn completion; the mocks
and synchronization are corrected, preserving the event-order assertion.
[Structured evidence](evidence/scenario-suite.json) retains every native attempt,
versions, exact commands, pre-fix failures and the intermediate validation result.

Native qualification predates the later cleanup/diagnostic/HA fixes; it is not a
native pass at final HEAD. Conservative source labels correctly require a rebuild
before another offline execution. Acceptance selection reports incomplete freeze,
fault and comparison prerequisites and runs no fixtures or holdouts. The latest
five-lane smoke gate remains open because of the vision refusal. Remaining mixed
faults, native resident missions, installed/exported tasks, strong comparisons,
separate release freezes and external reproduction remain unfinished.
