# Mission runtime audit

Baseline: `840fe959a07e8f2a95276f738228ff123d6d48c7`, clean tracked tree,
30 September 2026. The interface specification in `.planning/interface/`
supersedes the previous implementation work order for this change.

## Reuse

- `Session` is a thin asynchronous view of `OperationHost`. `owns_runtime=False`
  is already the default; only an owning session closes the host. `act`, `wait`,
  `cancel`, `inspect`, and `observe` are the supported boundary. Keep them.
- `OperationHost` owns admission, runtime epochs, request deduplication, effect
  exclusion, cancellation and the `indeterminate` outcome. Its request records
  are in memory. The application must journal intent before calling `act` and
  must never replay an unconfirmed physical request after process loss.
- Evidence is immutable, content-addressed and JSON-safe. Existing Harbor
  public evidence and private evaluator output remain separate and untouched.
- `HomeAssistantLight` is an existing real adapter with explicit identity,
  bounded requests, reported readback and uncertain-effect handling. A service
  connection can expose its state through an owned Session without pretending
  it is a person detector. Harbor remains available through injected Sessions.
- Existing argparse commands and tests retain their entry points and behavior.

## Adapt

- MCP `create_mcp_server(lifespan)` takes a Session; its lifespan can borrow a
  service-owned, scoped Session. Transport disconnect must not close the owner.
  Entryplug request IDs are independent of MCP transport IDs.
- A2A `create_a2a_server(session, base_url=...)` accepts an existing Session;
  inspect its shutdown before borrowing one and preserve task/operation IDs.
- OpenEnv owns an `EpisodeController` and requires resettable, owning Sessions
  with new runtime IDs on reset. Do not wrap a resident watch in an infinite
  step or hand it the service's owning Session. Add finite mission evaluation
  separately; preserve existing replay and episode tests.
- Harbor's resident factory owns its container/process lifetime through
  `ResidentSession.close`; keep that explicit ownership at application startup
  and shutdown rather than tying it to a browser or protocol connection.

## Add

A standard-library application layer with one asyncio owner, OS workspace
lock, SQLite WAL journal, revisioned definitions, bounded triggers/turns,
durable alerts and events; thin HTTP/SSE and Python clients; Solid console;
optional native inference/authentication adapters. The application operation
journal extends Session deduplication across the HTTP boundary, rather than
replacing admission or claiming exactly-once hardware effects.

The first free body is explicitly SIMULATED: labelled transitions distinguish
presence, entry, reentry, separate people, detector epochs and source failure.
Production middleware discovery is never inferred from names.

## Validation commands and initial environment

`python -m pytest tests`, `python -m pytest optional_tests/{mcp,a2a,openenv}`
when their extras are installed, `ruff check src tests`, `mypy`, and the new
service/browser contract checks. Container/ROS tests remain opt-in and require
their documented substrate. Initial shell: Python 3.10.12, no Node/npm, no
service/protocol/provider dependencies. Build/test runtimes are isolated in
`/tmp`; no system interpreter changes. Actual versions, results and unexecuted
checks are recorded in the delivery report, not assumed from the old manifest.
