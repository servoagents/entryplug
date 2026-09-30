import {
  createMemo,
  createSignal,
  For,
  lazy,
  onCleanup,
  onMount,
  Show,
  Suspense,
} from "solid-js";
import { render } from "solid-js/web";
import { api, authenticate, label, pretty, time, watchEvents } from "./api";
import type { Definition, RecordData, Snapshot } from "./api";
import "./style.css";

const Graph = lazy(() => import("./Graph"));
const terminal = new Set(["stopped", "completed", "failed"]);
const initialDefinition = (): Definition => ({
  name: "Watch the entrance",
  instructions:
    "Watch the entrance. Alert only on validated person entry; preserve temporal evidence. Missing observations never prove absence.",
  mode: "watch",
  schema_version: 1,
  agent: { driver: "rules", decision_mode: "rules", profile: "" },
  body: {
    selector: "demo.access-camera",
    required_capabilities: ["person.events"],
  },
  trigger: {
    kind: "event",
    source: "demo.access-camera",
    event: "person.entered",
    interval_s: 30,
    max_pending: 4,
    coalesce_window_ms: 500,
  },
  access: {
    allow: ["person.events", "camera.snapshot", "alerts.emit"],
    approve: [],
  },
});

function App() {
  const [snapshot, setSnapshot] = createSignal<Snapshot>();
  const [page, setPage] = createSignal(
    location.hash === "#connections" ? "connections" : "overview",
  );
  const [liveText, setLiveText] = createSignal<Record<string, string>>({});
  const [editedInstructions, setEditedInstructions] = createSignal<string>();
  const [selected, setSelected] = createSignal("");
  const [error, setError] = createSignal("");
  const [connected, setConnected] = createSignal(false);
  const [busy, setBusy] = createSignal(false);
  const [events, setEvents] = createSignal<RecordData[]>([]);
  const [profiles, setProfiles] = createSignal<RecordData[]>([]);
  const [detail, setDetail] = createSignal<RecordData>();
  const [node, setNode] = createSignal<RecordData>();
  const [search, setSearch] = createSignal("");
  const [step, setStep] = createSignal(0);
  const [definition, setDefinition] =
    createSignal<Definition>(initialDefinition());
  const [validated, setValidated] = createSignal<RecordData>();
  const [input, setInput] = createSignal("");
  const [connection, setConnection] = createSignal({
    name: "Home Assistant light",
    url: "ws://127.0.0.1:8123/api/websocket",
    entity_id: "light.entrance",
    token_env: "ENTRYPLUG_HA_TOKEN",
  });
  const [profileName, setProfileName] = createSignal("personal-chatgpt");
  const [model, setModel] = createSignal("");
  const [remoteKind, setRemoteKind] = createSignal("mcp");
  const [mcpUrl, setMcpUrl] = createSignal("http://127.0.0.1:8000/mcp");
  const [mcpTool, setMcpTool] = createSignal("");
  const [models, setModels] = createSignal<RecordData[]>([]);
  const [externalBody, setExternalBody] = createSignal("demo.access-camera");
  const [externalCaps, setExternalCaps] = createSignal<string[]>([
    "camera.snapshot",
  ]);
  const [attachmentSecret, setAttachmentSecret] = createSignal<RecordData>();
  let disconnect: (() => void) | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  let dialog!: HTMLDialogElement;
  const current = createMemo(() =>
    snapshot()?.runs.find((run) => run.id === selected()),
  );
  const mission = createMemo(() =>
    snapshot()?.missions.find((m) => m.id === current()?.mission_id),
  );
  const active = createMemo(
    () => snapshot()?.runs.filter((run) => !terminal.has(run.lifecycle)) ?? [],
  );
  const alerts = createMemo(
    () => snapshot()?.alerts.filter((alert) => !alert.acknowledged_at) ?? [],
  );
  const resources = createMemo(() =>
    (snapshot()?.topology.nodes ?? []).filter((item) =>
      `${item.label} ${item.id}`.toLowerCase().includes(search().toLowerCase()),
    ),
  );

  async function refresh() {
    const next = await api<Snapshot>("snapshot");
    if (next.build_id !== "mission-v1")
      throw new Error(
        "Service updated. Reload the console before using controls.",
      );
    setSnapshot(next);
    setProfiles(await api<RecordData[]>("profiles"));
  }
  async function reconnect() {
    disconnect?.();
    try {
      await authenticate();
      await refresh();
      if (disposed) return;
      setConnected(true);
      disconnect = watchEvents(
        snapshot()!.cursor,
        (event) => {
          if (event.type === "turn.text") {
            setLiveText((old) => ({
              ...Object.fromEntries(Object.entries(old).slice(-15)),
              [event.turn_id]: ((old[event.turn_id] || "") + event.delta).slice(
                -8000,
              ),
            }));
            return;
          }
          if (event.type === "turn.finished")
            setLiveText((old) => {
              const next = { ...old };
              delete next[event.turn_id];
              return next;
            });
          setEvents((old) => [...old, event].slice(-150));
          if (!refreshTimer)
            refreshTimer = setTimeout(() => {
              refreshTimer = undefined;
              refresh().catch(fail);
            }, 25);
        },
        () => {
          setConnected(false);
          if (!disposed) retry = setTimeout(reconnect, 1500);
        },
      );
    } catch (cause) {
      fail(cause);
      setConnected(false);
    }
  }
  function fail(cause: unknown) {
    setError(cause instanceof Error ? cause.message : "Request failed");
  }
  async function act(work: () => Promise<unknown>) {
    if (busy()) return;
    setBusy(true);
    setError("");
    try {
      await work();
      await refresh();
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(false);
    }
  }
  function inspect(value: RecordData) {
    if (value.body_id && value.capability)
      setNode(
        snapshot()?.topology.nodes.find(
          (n) => n.id === `${value.body_id}:${value.capability}`,
        ),
      );
    setDetail(value);
    dialog.showModal();
  }
  function go(value: string) {
    setPage(value);
    setError("");
  }
  function embark() {
    setDefinition(initialDefinition());
    setStep(0);
    setValidated(undefined);
    go("embark");
  }
  function patch(value: Partial<Definition>) {
    setDefinition((old) => ({ ...old, ...value }));
    setValidated(undefined);
  }
  async function review() {
    await act(async () => {
      setValidated(await api("missions/validate", "POST", definition()));
      setStep(3);
    });
  }
  async function launch() {
    await act(async () => {
      const created = await api("missions", "POST", validated()!.definition);
      const run = await api(`missions/${created.id}/runs`, "POST", {});
      setSelected(run.id);
      go("mission");
    });
  }
  async function showEvidence(id: string) {
    await act(async () => inspect(await api(`evidence/${id}`)));
  }
  onMount(reconnect);
  onCleanup(() => {
    disposed = true;
    disconnect?.();
    clearTimeout(retry);
    clearTimeout(refreshTimer);
  });

  return (
    <div class="shell">
      <a class="skip" href="#main">
        Skip to content
      </a>
      <aside class="sidebar">
        <a
          href="#"
          class="brand"
          onClick={(event) => {
            event.preventDefault();
            go("overview");
          }}
        >
          <span class="brand-mark">◇</span>
          <span>
            entryplug<small>DISTRIBUTED EMBODIMENT</small>
          </span>
        </a>
        <div class="workspace">
          <span class="eyebrow">WORKSPACE / LOCAL</span>
          <span class="workspace-name">
            Mission control <span class={connected() ? "dot live" : "dot"} />
          </span>
          <code>
            {snapshot()?.workspace_id.slice(0, 15) ?? "awaiting connection"}
          </code>
        </div>
        <nav aria-label="Main navigation">
          <button
            classList={{ chosen: page() === "overview" }}
            onClick={() => go("overview")}
          >
            <span>◈</span> Overview <small>01</small>
          </button>
          <button
            classList={{ chosen: page() === "mission" }}
            onClick={() => go("mission")}
          >
            <span>▱</span> Missions{" "}
            <small>{active().length.toString().padStart(2, "0")}</small>
          </button>
          <button
            classList={{ chosen: page() === "body" }}
            onClick={() => go("body")}
          >
            <span>⌘</span> Body & topology
          </button>
          <button
            classList={{ chosen: page() === "alerts" }}
            onClick={() => go("alerts")}
          >
            <span>△</span> Inbox{" "}
            <small>{alerts().length.toString().padStart(2, "0")}</small>
          </button>
          <button
            classList={{ chosen: page() === "connections" }}
            onClick={() => go("connections")}
          >
            <span>⊞</span> Connections
          </button>
        </nav>
        <div class="sidebar-footer">
          <span class="eyebrow">ONE OWNER. MANY INTERFACES.</span>
          <p>The service keeps working when this console closes.</p>
          <span class="mono faint">LOCAL SERVICE · API V1</span>
        </div>
      </aside>
      <div class="content">
        <header class="topbar">
          <span class="breadcrumb">
            WORKSPACE <span>/</span>{" "}
            {page() === "embark" ? "NEW MISSION" : page().toUpperCase()}
          </span>
          <span class="connection-status">
            <span class={connected() ? "dot live" : "dot"} />
            {connected() ? "Service connected" : "Service disconnected"}
          </span>
        </header>
        <main id="main">
          <Show when={error()}>
            <div class="error" role="alert">
              <span>{error()}</span>
              <button class="small" onClick={() => setError("")}>
                Dismiss
              </button>
            </div>
          </Show>
          <Show when={!snapshot()}>
            <section class="empty">
              <span class="large-mark">◇</span>
              <h1>Your body, your mission.</h1>
              <p>
                Open this console with <code>entryplug ui</code> to establish a
                private local session.
              </p>
              <button onClick={reconnect}>Reconnect</button>
            </section>
          </Show>
          <Show when={snapshot()}>
            <Show when={page() === "overview"}>
              <div class="page-heading">
                <div>
                  <span class="eyebrow">OPERATIONS / OVERVIEW</span>
                  <h1>A place to keep watch.</h1>
                  <p>Give an agent a body. Keep the mission in view.</p>
                </div>
                <button class="primary" onClick={embark}>
                  ◇ Embark <span>↗</span>
                </button>
              </div>
              <div class="metrics">
                <Metric
                  label="ACTIVE MISSIONS"
                  value={active().length}
                  note="Owned by the service"
                />
                <Metric
                  label="CONNECTED BODIES"
                  value={
                    snapshot()!.bodies.filter((b) => b.status === "available")
                      .length
                  }
                  note="Real and simulated, labelled"
                />
                <Metric
                  label="UNREAD ALERTS"
                  value={alerts().length}
                  note="Persisted in your inbox"
                />
                <Metric
                  label="MODEL CALLS"
                  value={snapshot()!.runs.reduce(
                    (sum, r) => sum + r.model_calls,
                    0,
                  )}
                  note="No calls while waiting for events"
                />
              </div>
              <div class="section-heading">
                <h2>
                  In the field <span class="muted">/ missions</span>
                </h2>
                <button class="text-button" onClick={() => go("mission")}>
                  All missions ↗
                </button>
              </div>
              <Show
                when={snapshot()!.runs.length}
                fallback={
                  <section class="empty empty-bordered">
                    <div class="instrument-mark">
                      ◇<span>＋</span>◇
                    </div>
                    <h2>The workspace is ready.</h2>
                    <p>
                      Start with a local rule and the simulated entrance camera.
                      <br />
                      No model account is needed.
                    </p>
                    <button onClick={embark}>
                      Create your first mission →
                    </button>
                  </section>
                }
              >
                <div class="mission-grid">
                  <For each={snapshot()!.runs.slice().reverse().slice(0, 6)}>
                    {(run) => (
                      <MissionCard
                        run={run}
                        open={() => {
                          setSelected(run.id);
                          go("mission");
                        }}
                      />
                    )}
                  </For>
                </div>
              </Show>
              <div class="overview-bottom">
                <section class="panel">
                  <div class="section-heading">
                    <h2>Available embodiment</h2>
                    <button
                      class="text-button"
                      onClick={() => go("connections")}
                    >
                      Connect body ↗
                    </button>
                  </div>
                  <For each={snapshot()!.bodies}>
                    {(body) => (
                      <button class="body-row" onClick={() => go("body")}>
                        <span class="body-symbol">◇</span>
                        <span>
                          <strong>{body.name}</strong>
                          <small>
                            {body.capabilities.length} capabilities ·{" "}
                            {body.status}
                          </small>
                        </span>
                        <span class="tag">
                          {body.simulated
                            ? "SIMULATED"
                            : body.provenance.toUpperCase()}
                        </span>
                      </button>
                    )}
                  </For>
                </section>
                <section class="panel">
                  <div class="section-heading">
                    <h2>Latest signals</h2>
                    <span class="eyebrow">PERSISTENT INBOX</span>
                  </div>
                  <Show
                    when={alerts().length}
                    fallback={
                      <p class="quiet">
                        No pending alerts. Waiting is a valid state.
                      </p>
                    }
                  >
                    <For each={alerts().slice(-3).reverse()}>
                      {(alert) => (
                        <div class="signal-row">
                          <span class="amber">△</span>
                          <div>
                            <strong>{alert.message}</strong>
                            <small>{time(alert.at)} · evidence attached</small>
                          </div>
                        </div>
                      )}
                    </For>
                  </Show>
                </section>
              </div>
            </Show>

            <Show when={page() === "embark"}>
              <div class="page-heading">
                <div>
                  <span class="eyebrow">NEW MISSION</span>
                  <h1>Embark.</h1>
                  <p>A clear purpose. An explicit body. A bounded scope.</p>
                </div>
                <button onClick={() => go("overview")}>Cancel</button>
              </div>
              <ol class="steps">
                <For
                  each={[
                    "Who acts",
                    "Choose a body",
                    "Give a mission",
                    "Review & launch",
                  ]}
                >
                  {(name, index) => (
                    <li
                      classList={{
                        current: step() === index(),
                        done: step() > index(),
                      }}
                    >
                      <span>{index() + 1}</span>
                      {name}
                    </li>
                  )}
                </For>
              </ol>
              <section class="wizard panel">
                <Show when={step() === 0}>
                  <span class="eyebrow">01 / WHO ACTS</span>
                  <h2>Choose the decision maker.</h2>
                  <div class="choice-grid">
                    <button
                      classList={{
                        selected: definition().agent?.driver === "rules",
                      }}
                      onClick={() =>
                        patch({
                          agent: {
                            driver: "rules",
                            decision_mode: "rules",
                            profile: "",
                          },
                        })
                      }
                    >
                      <span>⌁</span>
                      <h3>Local rule</h3>
                      <p>
                        Respond to a validated entry event. Deterministic,
                        local, no model calls.
                      </p>
                      <small>READY · NO ACCOUNT</small>
                    </button>
                    <button
                      classList={{
                        selected: definition().agent?.driver === "native",
                      }}
                      disabled={!profiles().some((p) => p.status === "ready")}
                      onClick={() =>
                        patch({
                          agent: {
                            driver: "native",
                            decision_mode: "assisted",
                            profile: profiles().find(
                              (p) => p.status === "ready",
                            )?.id,
                          },
                        })
                      }
                    >
                      <span>◇</span>
                      <h3>Entryplug agent</h3>
                      <p>
                        Bounded reasoning with a configured OpenAI or ChatGPT
                        profile.
                      </p>
                      <small>
                        {profiles().some((p) => p.status === "ready")
                          ? "PROFILE CONNECTED"
                          : "CONNECT A PROFILE FIRST"}
                      </small>
                    </button>
                    <button onClick={() => go("connections")}>
                      <span>↗</span>
                      <h3>External agent</h3>
                      <p>
                        Grant a scoped API or MCP connection. Its agent loop
                        stays external.
                      </p>
                      <small>MANAGE CONNECTIONS ↗</small>
                    </button>
                  </div>
                  <Show when={definition().agent?.driver === "native"}>
                    <label>
                      Inference profile
                      <select
                        value={definition().agent?.profile}
                        onChange={(e) =>
                          patch({
                            agent: {
                              driver: "native",
                              decision_mode: "assisted",
                              profile: e.currentTarget.value,
                            },
                          })
                        }
                      >
                        <For
                          each={profiles().filter((p) => p.status === "ready")}
                        >
                          {(p) => (
                            <option value={p.id}>
                              {p.id} · {p.provider} · {p.model}
                            </option>
                          )}
                        </For>
                      </select>
                    </label>
                    <p class="notice">
                      Selected observations and instructions are sent to this
                      provider. Connecting inference does not grant physical
                      permissions.
                    </p>
                  </Show>
                </Show>
                <Show when={step() === 1}>
                  <span class="eyebrow">02 / EMBODIMENT</span>
                  <h2>Choose what it can access.</h2>
                  <label>
                    Body
                    <select
                      value={definition().body?.selector}
                      onChange={(e) => {
                        const body = snapshot()!.bodies.find(
                          (b) => b.id === e.currentTarget.value,
                        )!;
                        patch({
                          body: {
                            selector: body.id,
                            required_capabilities: [body.capabilities[0].name],
                          },
                          trigger: {
                            ...definition().trigger,
                            source: body.id,
                            kind: body.simulated ? "event" : "manual",
                          },
                          access: {
                            allow: [
                              ...body.capabilities.map(
                                (c: RecordData) => c.name,
                              ),
                              "alerts.emit",
                            ],
                            approve: [],
                          },
                        });
                      }}
                    >
                      <For each={snapshot()!.bodies}>
                        {(body) => (
                          <option value={body.id}>
                            {body.name} · {body.status}
                          </option>
                        )}
                      </For>
                    </select>
                  </label>
                  <p class="notice">
                    The demo uses labelled transitions. It is SIMULATED and does
                    not detect people from a real camera.
                  </p>
                  <fieldset>
                    <legend>Allowed capabilities</legend>
                    <For
                      each={[
                        ...(snapshot()!
                          .bodies.find(
                            (b) => b.id === definition().body?.selector,
                          )
                          ?.capabilities.map((c: RecordData) => c.name) ?? []),
                        "alerts.emit",
                      ]}
                    >
                      {(cap) => (
                        <label class="check">
                          <input
                            type="checkbox"
                            checked={definition().access?.allow?.includes(cap)}
                            onChange={(e) =>
                              patch({
                                access: {
                                  ...definition().access,
                                  allow: e.currentTarget.checked
                                    ? [...definition().access!.allow!, cap]
                                    : definition().access!.allow!.filter(
                                        (c) => c !== cap,
                                      ),
                                },
                              })
                            }
                          />
                          <code>{cap}</code>
                        </label>
                      )}
                    </For>
                  </fieldset>
                </Show>
                <Show when={step() === 2}>
                  <span class="eyebrow">03 / PURPOSE</span>
                  <h2>Make the mission explicit.</h2>
                  <label>
                    Mission name
                    <input
                      value={definition().name}
                      maxLength={160}
                      onInput={(e) => patch({ name: e.currentTarget.value })}
                    />
                  </label>
                  <label>
                    Instructions
                    <textarea
                      rows={6}
                      value={definition().instructions}
                      onInput={(e) =>
                        patch({ instructions: e.currentTarget.value })
                      }
                    />
                  </label>
                  <label class="file-label">
                    Load instructions (.md or .txt)
                    <input
                      type="file"
                      accept=".md,.txt"
                      onChange={async (e) => {
                        const file = e.currentTarget.files?.[0];
                        if (file) {
                          if (file.size > 65536)
                            return setError("Instructions exceed 64 KiB");
                          patch({ instructions: await file.text() });
                        }
                      }}
                    />
                  </label>
                  <div class="form-row">
                    <label>
                      Mission type
                      <select
                        value={definition().mode}
                        onChange={(e) =>
                          patch({
                            mode: e.currentTarget.value as "watch" | "once",
                          })
                        }
                      >
                        <option value="watch">
                          Watch · stays active between turns
                        </option>
                        <option value="once">Once · one bounded turn</option>
                      </select>
                    </label>
                    <label>
                      Trigger
                      <select
                        value={definition().trigger?.kind}
                        onChange={(e) =>
                          patch({
                            trigger: {
                              ...definition().trigger,
                              kind: e.currentTarget.value as
                                "event" | "manual" | "interval",
                            },
                          })
                        }
                      >
                        <option value="event">
                          Event · validated person entry
                        </option>
                        <option value="manual">Manual</option>
                        <option value="interval">
                          Interval · explicit sampling
                        </option>
                      </select>
                    </label>
                  </div>
                  <Show when={definition().trigger?.kind === "interval"}>
                    <label>
                      Interval in seconds
                      <input
                        type="number"
                        min="0.1"
                        max="86400"
                        value={definition().trigger?.interval_s}
                        onInput={(e) =>
                          patch({
                            trigger: {
                              ...definition().trigger,
                              interval_s: Number(e.currentTarget.value),
                            },
                          })
                        }
                      />
                    </label>
                    <p class="notice">
                      Sampling can miss events between checks. A single
                      observation supports presence, not entry.
                    </p>
                  </Show>
                  <details>
                    <summary>Advanced limits</summary>
                    <p>
                      One active turn per mission · 45 s per turn · 3 model
                      calls · 6 tools · 60 turns per hour · queue of 4.
                    </p>
                    <p>
                      Source loss blocks the mission. Recovery requires
                      revalidation.
                    </p>
                  </details>
                </Show>
                <Show when={step() === 3 && validated()}>
                  <span class="eyebrow">04 / REVIEW</span>
                  <h2>Ready to keep watch.</h2>
                  <dl class="review">
                    <dt>Purpose</dt>
                    <dd>{definition().name}</dd>
                    <dt>Body</dt>
                    <dd>{definition().body?.selector}</dd>
                    <dt>Decision maker</dt>
                    <dd>
                      {definition().agent?.driver} {definition().agent?.profile}
                    </dd>
                    <dt>Wake on</dt>
                    <dd>
                      {definition().trigger?.kind} ·{" "}
                      {definition().trigger?.event}
                    </dd>
                    <dt>May use</dt>
                    <dd>{definition().access?.allow?.join(", ")}</dd>
                    <dt>Results</dt>
                    <dd>
                      Persistent Entryplug inbox, with evidence references
                    </dd>
                  </dl>
                  <p class="notice">
                    Closing this tab leaves the mission running. Pausing closes
                    new admissions; it does not guarantee a physical stop.
                  </p>
                  <details>
                    <summary>Validated configuration</summary>
                    <pre>{pretty(validated()!.definition)}</pre>
                  </details>
                </Show>
                <div class="wizard-actions">
                  <button
                    disabled={step() === 0 || busy()}
                    onClick={() => setStep(step() - 1)}
                  >
                    ← Back
                  </button>
                  <Show when={step() < 2}>
                    <button class="primary" onClick={() => setStep(step() + 1)}>
                      Continue →
                    </button>
                  </Show>
                  <Show when={step() === 2}>
                    <button class="primary" disabled={busy()} onClick={review}>
                      Validate & review →
                    </button>
                  </Show>
                  <Show when={step() === 3}>
                    <button
                      class="primary"
                      disabled={busy() || !connected()}
                      onClick={launch}
                    >
                      {busy() ? "Launching…" : "Launch mission ↗"}
                    </button>
                  </Show>
                </div>
              </section>
            </Show>

            <Show when={page() === "mission"}>
              <div class="page-heading">
                <div>
                  <span class="eyebrow">MISSION OPERATIONS</span>
                  <h1>{current()?.name ?? "Your missions."}</h1>
                  <p>
                    {current()
                      ? `${current()!.id} · revision ${current()!.revision}`
                      : "Select a run to inspect activity, evidence, and its body."}
                  </p>
                </div>
                <button class="primary" onClick={embark}>
                  ◇ Embark
                </button>
              </div>
              <Show when={snapshot()!.runs.length}>
                <label class="run-picker">
                  Execution
                  <select
                    aria-label="Execution"
                    value={selected()}
                    onChange={(e) => {
                      setSelected(e.currentTarget.value);
                      setEditedInstructions(undefined);
                    }}
                  >
                    <option value="">Select a mission run</option>
                    <For each={snapshot()!.runs}>
                      {(run) => (
                        <option value={run.id} selected={selected() === run.id}>
                          {run.name} · {label(run.lifecycle)} ·{" "}
                          {run.id.slice(-6)}
                        </option>
                      )}
                    </For>
                  </select>
                </label>
              </Show>
              <Show when={current()}>
                {(run) => (
                  <>
                    <section class="run-status">
                      <div>
                        <span
                          class="status"
                          classList={{ fault: run().health === "blocked" }}
                        >
                          <span class="dot live" />
                          {label(run().lifecycle).toUpperCase()} ·{" "}
                          {label(run().activity)}
                        </span>
                        <small>
                          {run().health === "ok"
                            ? "Body validated"
                            : `Attention: ${run().reason_code}`}{" "}
                          · Last observation {time(run().last_observation?.at)}
                        </small>
                      </div>
                      <div class="actions">
                        <button
                          disabled={
                            busy() ||
                            !connected() ||
                            terminal.has(run().lifecycle)
                          }
                          onClick={() =>
                            act(() =>
                              api(
                                `runs/${run().id}/${run().lifecycle === "paused" || run().health === "blocked" ? "resume" : "pause"}`,
                                "POST",
                                {},
                              ),
                            )
                          }
                        >
                          {run().lifecycle === "paused" ||
                          run().health === "blocked"
                            ? "Resume / revalidate"
                            : "Pause"}
                        </button>
                        <button
                          class="danger"
                          disabled={
                            busy() ||
                            !connected() ||
                            terminal.has(run().lifecycle)
                          }
                          onClick={() =>
                            act(() => api(`runs/${run().id}/stop`, "POST", {}))
                          }
                        >
                          Stop mission
                        </button>
                      </div>
                    </section>
                    <Show
                      when={snapshot()!.operations.some(
                        (op) =>
                          op.run_id === run().id &&
                          op.physical_effects &&
                          [
                            "indeterminate",
                            "accepted",
                            "running",
                            "canceling",
                          ].includes(op.lifecycle),
                      )}
                    >
                      <p class="notice" role="status">
                        {run().lifecycle === "stopped"
                          ? "Mission stopped; physical state is not confirmed."
                          : "A physical operation is unresolved."}{" "}
                        Inspect its evidence. An indeterminate resource blocks
                        further writes.
                      </p>
                    </Show>
                    <For
                      each={snapshot()!.approvals.filter(
                        (a) => a.run_id === run().id && a.status === "pending",
                      )}
                    >
                      {(approval) => (
                        <section class="panel">
                          <h2>Approval requested</h2>
                          <p>
                            Revision {approval.revision} · {approval.body_id} ·
                            expires{" "}
                            {new Date(
                              approval.expires_at * 1000,
                            ).toLocaleTimeString()}
                          </p>
                          <pre>
                            {pretty(
                              snapshot()!.operations.find(
                                (op) => op.id === approval.operation_id,
                              ),
                            )}
                          </pre>
                          <div class="actions">
                            <button
                              disabled={busy()}
                              onClick={() =>
                                act(() =>
                                  api(
                                    `approvals/${approval.id}/decision`,
                                    "POST",
                                    { allow: true },
                                  ),
                                )
                              }
                            >
                              Approve this operation
                            </button>
                            <button
                              disabled={busy()}
                              onClick={() =>
                                act(() =>
                                  api(
                                    `approvals/${approval.id}/decision`,
                                    "POST",
                                    { allow: false },
                                  ),
                                )
                              }
                            >
                              Deny
                            </button>
                          </div>
                        </section>
                      )}
                    </For>
                    <div class="mission-columns">
                      <section class="panel">
                        <div class="section-heading">
                          <h2>Embodiment</h2>
                          <span class="eyebrow">
                            {run().body_id.startsWith("demo.")
                              ? "SIMULATED"
                              : "OBSERVED"}
                          </span>
                        </div>
                        <Suspense fallback={<p>Loading body graph…</p>}>
                          <Graph
                            topology={{
                              nodes: snapshot()!.topology.nodes.filter(
                                (n) => n.origin === run().body_id,
                              ),
                              edges: snapshot()!.topology.edges.filter(
                                (n) => n.origin === run().body_id,
                              ),
                            }}
                            select={setNode}
                          />
                        </Suspense>
                        <p class="muted">{node()?.label ?? run().body_id}</p>
                        <div class="observation">
                          <span class="eyebrow">LATEST OBSERVATION</span>
                          <p>
                            {run().reason_code === "source_lost" ||
                            run().reason_code === "stale_observation"
                              ? "Coverage unavailable. The last observation does not establish the current scene."
                              : run().last_observation
                                ? `${label(run().last_observation!.type)} · ${run().last_observation!.present?.length ?? "?"} present`
                                : "No observation yet. Waiting for the source."}
                          </p>
                          <small>
                            Source: {run().last_observation?.source ?? "—"} ·{" "}
                            {time(run().last_observation?.at)}
                          </small>
                        </div>
                        <Show when={run().body_id === "demo.access-camera"}>
                          <details>
                            <summary>Simulated source controls</summary>
                            <p>
                              Advance the labelled fixture. These controls do
                              not operate a real camera.
                            </p>
                            <div class="demo-buttons">
                              <For
                                each={[
                                  ["enter_a", "Person A enters"],
                                  ["presence_a", "Continued presence"],
                                  ["enter_b", "Person B enters"],
                                  ["exit_a", "Person A leaves"],
                                  ["lost", "Lose source"],
                                  ["recover", "Recover source"],
                                ]}
                              >
                                {([action, name]) => (
                                  <button
                                    class="small"
                                    disabled={busy()}
                                    onClick={() =>
                                      act(() =>
                                        api("demo/step", "POST", { action }),
                                      )
                                    }
                                  >
                                    {name}
                                  </button>
                                )}
                              </For>
                            </div>
                          </details>
                        </Show>
                      </section>
                      <section class="panel activity-panel">
                        <div class="section-heading">
                          <h2>Activity</h2>
                          <span class="eyebrow">PUBLIC EVENTS</span>
                        </div>
                        <div class="activity-list">
                          <Show
                            when={snapshot()!.turns.some(
                              (t) => t.run_id === run().id,
                            )}
                            fallback={
                              <div class="waiting">
                                <span>⌁</span>
                                <h3>Waiting for an event</h3>
                                <p>
                                  The mission is alive. No model is running in
                                  the background.
                                </p>
                              </div>
                            }
                          >
                            <For
                              each={snapshot()!
                                .turns.filter((t) => t.run_id === run().id)
                                .slice(-15)
                                .reverse()}
                            >
                              {(turn) => (
                                <article class="activity-item">
                                  <time>{time(turn.created_at)}</time>
                                  <div>
                                    <strong>
                                      {label(turn.status)} turn{" "}
                                      <span class="muted">
                                        · r{turn.revision}
                                      </span>
                                    </strong>
                                    <p>
                                      {turn.result ||
                                        turn.reason_code ||
                                        liveText()[turn.id] ||
                                        "Working…"}
                                    </p>
                                    <Show
                                      when={
                                        turn.status === "running" &&
                                        liveText()[turn.id]
                                      }
                                    >
                                      <small>
                                        Streaming text · awaiting confirmed
                                        completion
                                      </small>
                                    </Show>
                                    <small>
                                      {turn.model_calls} model calls ·{" "}
                                      {turn.tool_calls} tools
                                    </small>
                                  </div>
                                </article>
                              )}
                            </For>
                          </Show>
                        </div>
                        <form
                          onSubmit={(e) => {
                            e.preventDefault();
                            act(async () => {
                              await api(`runs/${run().id}/inputs`, "POST", {
                                text: input(),
                                target: "next_turn",
                              });
                              setInput("");
                            });
                          }}
                        >
                          <label>
                            Instruction for the next turn
                            <textarea
                              rows={2}
                              value={input()}
                              onInput={(e) => setInput(e.currentTarget.value)}
                              placeholder="Add an instruction to the next bounded turn…"
                            />
                          </label>
                          <button
                            disabled={
                              busy() ||
                              !input().trim() ||
                              terminal.has(run().lifecycle)
                            }
                          >
                            Send instruction ↗
                          </button>
                        </form>
                      </section>
                    </div>
                    <div class="panel">
                      <div class="section-heading">
                        <h2>Operations & evidence</h2>
                        <span class="eyebrow">
                          ACCEPTANCE ≠ CONFIRMED EFFECT
                        </span>
                      </div>
                      <Show
                        when={snapshot()!.operations.some(
                          (op) => op.run_id === run().id,
                        )}
                        fallback={
                          <p class="quiet">
                            No body operations admitted for this run.
                          </p>
                        }
                      >
                        <div class="table-wrap">
                          <table>
                            <thead>
                              <tr>
                                <th>Capability</th>
                                <th>Operation</th>
                                <th>Effect knowledge</th>
                                <th>Evidence</th>
                              </tr>
                            </thead>
                            <tbody>
                              <For
                                each={snapshot()!.operations.filter(
                                  (op) => op.run_id === run().id,
                                )}
                              >
                                {(op) => (
                                  <tr>
                                    <td>{op.capability}</td>
                                    <td>
                                      <button
                                        class="text-button"
                                        onClick={() => inspect(op)}
                                      >
                                        {label(op.lifecycle)} ↗
                                      </button>
                                    </td>
                                    <td>{op.effect_state ?? "unknown"}</td>
                                    <td>
                                      <For each={op.evidence_ids}>
                                        {(id: string) => (
                                          <button
                                            class="text-button"
                                            onClick={() => showEvidence(id)}
                                          >
                                            Inspect ↗
                                          </button>
                                        )}
                                      </For>
                                    </td>
                                  </tr>
                                )}
                              </For>
                            </tbody>
                          </table>
                        </div>
                      </Show>
                      <For
                        each={snapshot()!.alerts.filter(
                          (a) => a.run_id === run().id,
                        )}
                      >
                        {(alert) => (
                          <div class="signal-row">
                            <span class="amber">△</span>
                            <div>
                              <strong>{alert.message}</strong>
                              <small>{time(alert.at)}</small>
                            </div>
                            <For each={alert.evidence_ids}>
                              {(id: string) => (
                                <button
                                  class="text-button"
                                  onClick={() => showEvidence(id)}
                                >
                                  Evidence ↗
                                </button>
                              )}
                            </For>
                          </div>
                        )}
                      </For>
                      <details>
                        <summary>Edit instructions for future turns</summary>
                        <p>
                          A saved change creates a revision. Any active turn
                          keeps its original instructions.
                        </p>
                        <label>
                          Permanent mission instructions
                          <textarea
                            rows={5}
                            value={
                              editedInstructions() ??
                              mission()?.definition.instructions ??
                              ""
                            }
                            onInput={(e) =>
                              setEditedInstructions(e.currentTarget.value)
                            }
                          />
                        </label>
                        <button
                          disabled={busy() || !editedInstructions()?.trim()}
                          onClick={() =>
                            act(async () => {
                              await api(
                                `missions/${mission()!.id}`,
                                "PATCH",
                                {
                                  definition: {
                                    ...mission()!.definition,
                                    instructions: editedInstructions(),
                                  },
                                },
                                mission()!.revision,
                              );
                              setEditedInstructions(undefined);
                            })
                          }
                        >
                          Save new revision
                        </button>
                      </details>
                      <details>
                        <summary>Definition and measured usage</summary>
                        <pre>
                          {pretty({
                            definition: mission()?.definition,
                            revision: run().revision,
                            model_calls: run().model_calls,
                            tool_calls: run().tool_calls,
                          })}
                        </pre>
                      </details>
                      <details>
                        <summary>Recent service events</summary>
                        <pre>
                          {pretty(
                            events().filter((e) => e.run_id === run().id),
                          )}
                        </pre>
                      </details>
                    </div>
                  </>
                )}
              </Show>
            </Show>

            <Show when={page() === "body"}>
              <div class="page-heading">
                <div>
                  <span class="eyebrow">DISTRIBUTED BODY</span>
                  <h1>Connections, with context.</h1>
                  <p>
                    Observed and configured relationships retain their origin.
                    Nothing here implies physical wiring.
                  </p>
                </div>
                <button onClick={() => go("connections")}>
                  Connect body ↗
                </button>
              </div>
              <section class="panel">
                <Suspense fallback={<p>Loading topology…</p>}>
                  <Graph
                    topology={snapshot()!.topology}
                    select={setNode}
                    selectedId={node()?.id}
                  />
                </Suspense>
                <Show when={node()}>
                  <div class="node-detail">
                    <strong>{node()!.label}</strong>
                    <span>
                      {node()!.kind} · {node()!.provenance} · {node()!.status}
                    </span>
                    <code>{node()!.id}</code>
                  </div>
                </Show>
                <label>
                  Find a resource
                  <input
                    type="search"
                    value={search()}
                    onInput={(e) => setSearch(e.currentTarget.value)}
                    placeholder="Name, capability, or resource ID"
                  />
                </label>
                <div class="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Resource</th>
                        <th>Kind</th>
                        <th>Provenance</th>
                        <th>Last seen</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={resources()}>
                        {(item) => (
                          <tr>
                            <td>
                              <button
                                class="text-button"
                                onClick={() => {
                                  setNode(item);
                                  inspect(item);
                                }}
                              >
                                {item.label} ↗
                              </button>
                            </td>
                            <td>{item.kind}</td>
                            <td>
                              {item.provenance}
                              {item.simulated ? " · SIMULATED" : ""}
                            </td>
                            <td>{time(item.last_seen)}</td>
                            <td>{item.status}</td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </div>
              </section>
            </Show>

            <Show when={page() === "alerts"}>
              <div class="page-heading">
                <div>
                  <span class="eyebrow">PERSISTENT INBOX</span>
                  <h1>What happened.</h1>
                  <p>Alerts outlive tabs, observers, and service restarts.</p>
                </div>
                <span class="tag">{alerts().length} UNREAD</span>
              </div>
              <section class="panel">
                <Show
                  when={snapshot()!.alerts.length}
                  fallback={<p class="quiet">No alerts yet.</p>}
                >
                  <For each={snapshot()!.alerts.slice().reverse()}>
                    {(alert) => (
                      <article class="inbox-item">
                        <span class="amber">△</span>
                        <div>
                          <strong>{alert.message}</strong>
                          <p>
                            {time(alert.at)} ·{" "}
                            {alert.acknowledged_at ? "Acknowledged" : "Unread"}{" "}
                            · inbox delivery confirmed
                          </p>
                          <button
                            class="text-button"
                            onClick={() => {
                              setSelected(alert.run_id);
                              go("mission");
                            }}
                          >
                            Open mission ↗
                          </button>
                          <For each={alert.evidence_ids}>
                            {(id: string) => (
                              <button
                                class="text-button"
                                onClick={() => showEvidence(id)}
                              >
                                Inspect evidence ↗
                              </button>
                            )}
                          </For>
                        </div>
                        <button
                          disabled={busy() || !!alert.acknowledged_at}
                          onClick={() =>
                            act(() => api(`alerts/${alert.id}/ack`, "POST", {}))
                          }
                        >
                          Acknowledge
                        </button>
                      </article>
                    )}
                  </For>
                </Show>
              </section>
            </Show>

            <Show when={page() === "connections"}>
              <div class="page-heading">
                <div>
                  <span class="eyebrow">CONNECTIONS & AGENTS</span>
                  <h1>Make access explicit.</h1>
                  <p>
                    Inference profiles and body permissions are separate
                    decisions.
                  </p>
                </div>
              </div>
              <div class="connections-grid">
                <section class="panel">
                  <span class="eyebrow">INFERENCE</span>
                  <h2>Bring your model.</h2>
                  <For each={profiles()}>
                    {(profile) => (
                      <div class="profile-row">
                        <div>
                          <strong>{profile.id}</strong>
                          <small>
                            {profile.provider} ·{" "}
                            {profile.model || "Choose a model"} ·{" "}
                            {label(profile.status)}
                          </small>
                        </div>
                        <button
                          class="small"
                          onClick={() =>
                            act(() =>
                              api("auth/logout", "POST", {
                                profile: profile.id,
                              }),
                            )
                          }
                        >
                          Disconnect
                        </button>
                      </div>
                    )}
                  </For>
                  <label>
                    Profile name
                    <input
                      value={profileName()}
                      onInput={(e) => setProfileName(e.currentTarget.value)}
                    />
                  </label>
                  <button
                    class="chatgpt"
                    disabled={busy()}
                    onClick={() =>
                      act(async () => {
                        const login = await api("auth/chatgpt/start", "POST", {
                          profile: profileName(),
                        });
                        location.assign(login.authorization_url);
                      })
                    }
                  >
                    Continue with ChatGPT
                  </button>
                  <p class="muted">
                    Uses your eligible plan. Availability and quotas depend on
                    your account. No physical access is granted.
                  </p>
                  <div class="form-row">
                    <label>
                      Model
                      <input
                        list="model-options"
                        value={model()}
                        onInput={(e) => setModel(e.currentTarget.value)}
                        placeholder="Choose an available model"
                      />
                      <datalist id="model-options">
                        <For each={models()}>
                          {(m) => <option value={m.id}>{m.name}</option>}
                        </For>
                      </datalist>
                    </label>
                    <button
                      class="small"
                      onClick={() =>
                        act(async () =>
                          setModels(
                            await api<RecordData[]>(
                              `profiles/${profileName()}/models`,
                            ),
                          ),
                        )
                      }
                    >
                      Load models
                    </button>
                  </div>
                  <div class="actions">
                    <button
                      disabled={!model()}
                      onClick={() =>
                        act(() =>
                          api("profiles", "POST", {
                            id: profileName(),
                            provider: "chatgpt",
                            model: model(),
                          }),
                        )
                      }
                    >
                      Use with ChatGPT
                    </button>
                    <button
                      disabled={!model()}
                      onClick={() =>
                        act(() =>
                          api("profiles", "POST", {
                            id: profileName(),
                            provider: "openai",
                            model: model(),
                            api_key_env: "OPENAI_API_KEY",
                          }),
                        )
                      }
                    >
                      Use API key
                    </button>
                  </div>
                  <p class="fine-print">
                    API-key profiles read OPENAI_API_KEY from the service
                    environment. Selected observations and context leave this
                    machine for remote inference. Secrets remain in the backend.
                  </p>
                </section>
                <section class="panel">
                  <span class="eyebrow">EMBODIMENT</span>
                  <h2>Connect an existing body.</h2>
                  <p>
                    Home Assistant light state. Read-only, with device-reported
                    evidence.
                  </p>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      act(() =>
                        api("connections", "POST", {
                          kind: "homeassistant",
                          ...connection(),
                        }),
                      );
                    }}
                  >
                    <For
                      each={[
                        ["name", "Connection name"],
                        ["url", "WebSocket URL"],
                        ["entity_id", "Allowed light entity"],
                        ["token_env", "Token environment variable"],
                      ]}
                    >
                      {([key, name]) => (
                        <label>
                          {name}
                          <input
                            value={(connection() as RecordData)[key]}
                            onInput={(e) =>
                              setConnection((old) => ({
                                ...old,
                                [key]: e.currentTarget.value,
                              }))
                            }
                          />
                        </label>
                      )}
                    </For>
                    <button disabled={busy()}>Connect & verify ↗</button>
                  </form>
                  <details>
                    <summary>Import read-only remote tools</summary>
                    <label>
                      Protocol
                      <select
                        value={remoteKind()}
                        onChange={(e) => setRemoteKind(e.currentTarget.value)}
                      >
                        <option value="mcp">MCP tools</option>
                        <option value="a2a">
                          A2A · one-hop Entryplug capabilities
                        </option>
                      </select>
                    </label>
                    <p>
                      Only explicitly selected tools marked read-only are
                      imported. The observed schema is pinned until reconnect.
                      Tool content cannot expand mission access.
                    </p>
                    <label>
                      Endpoint or Agent Card URL
                      <input
                        value={mcpUrl()}
                        onInput={(e) => setMcpUrl(e.currentTarget.value)}
                      />
                    </label>
                    <label>
                      Tool names (comma separated)
                      <input
                        value={mcpTool()}
                        onInput={(e) => setMcpTool(e.currentTarget.value)}
                      />
                    </label>
                    <p class="fine-print">
                      If needed, set{" "}
                      {remoteKind() === "mcp"
                        ? "ENTRYPLUG_MCP_TOKEN"
                        : "ENTRYPLUG_A2A_TOKEN"}{" "}
                      in the service environment.
                    </p>
                    <button
                      disabled={busy() || !mcpTool().trim()}
                      onClick={() =>
                        act(() =>
                          api("connections", "POST", {
                            kind: remoteKind(),
                            name: "Remote tools",
                            url: mcpUrl(),
                            tools: mcpTool()
                              .split(",")
                              .map((s) => s.trim()),
                            token_env:
                              remoteKind() === "mcp"
                                ? "ENTRYPLUG_MCP_TOKEN"
                                : "ENTRYPLUG_A2A_TOKEN",
                          }),
                        )
                      }
                    >
                      Connect remote tools
                    </button>
                  </details>
                  <For each={snapshot()!.connections}>
                    {(item) => (
                      <div class="profile-row">
                        <div>
                          <strong>{item.name}</strong>
                          <small>
                            {item.status} ·{" "}
                            {item.reason_code || item.provenance}
                          </small>
                        </div>
                        <button
                          class="small"
                          onClick={() =>
                            act(() =>
                              api(`connections/${item.id}`, "DELETE", {}),
                            )
                          }
                        >
                          Disconnect
                        </button>
                      </div>
                    )}
                  </For>
                </section>
                <section class="panel external-panel">
                  <span class="eyebrow">EXTERNAL CONTROL</span>
                  <h2>Attach an external agent.</h2>
                  <p>
                    Entryplug grants body access. Your agent owns its login and
                    reasoning loop.
                  </p>
                  <div class="form-row">
                    <label>
                      Body
                      <select
                        value={externalBody()}
                        onChange={(e) => {
                          setExternalBody(e.currentTarget.value);
                          setExternalCaps([]);
                        }}
                      >
                        <For each={snapshot()!.bodies}>
                          {(body) => (
                            <option value={body.id}>{body.name}</option>
                          )}
                        </For>
                      </select>
                    </label>
                    <fieldset>
                      <legend>Grant capabilities</legend>
                      <For
                        each={
                          snapshot()!.bodies.find(
                            (b) => b.id === externalBody(),
                          )?.capabilities ?? []
                        }
                      >
                        {(cap) => (
                          <label class="check">
                            <input
                              type="checkbox"
                              checked={externalCaps().includes(cap.name)}
                              onChange={(e) =>
                                setExternalCaps((old) =>
                                  e.currentTarget.checked
                                    ? [...old, cap.name]
                                    : old.filter((c) => c !== cap.name),
                                )
                              }
                            />
                            <code>{cap.name}</code>
                          </label>
                        )}
                      </For>
                    </fieldset>
                  </div>
                  <button
                    disabled={!externalCaps().length || busy()}
                    onClick={() =>
                      act(async () =>
                        setAttachmentSecret(
                          await api("attachments", "POST", {
                            name: "External agent",
                            body_id: externalBody(),
                            allow: externalCaps(),
                          }),
                        ),
                      )
                    }
                  >
                    Create scoped access ↗
                  </button>
                  <Show when={attachmentSecret()}>
                    <div class="secret">
                      <strong>
                        Save this credential in your client's secret store.
                      </strong>
                      <p>
                        It is only shown for this attachment creation. Dismiss
                        when saved.
                      </p>
                      <p>
                        MCP:{" "}
                        <code>
                          {location.origin}/mcp/{attachmentSecret()!.id}/
                        </code>
                      </p>
                      <p>
                        A2A:{" "}
                        <code>
                          {location.origin}/a2a/{attachmentSecret()!.id}
                          /.well-known/agent-card.json
                        </code>
                      </p>
                      <p>
                        Pass the credential as an Authorization: Bearer header.
                      </p>
                      <pre>{pretty(attachmentSecret())}</pre>
                      <button onClick={() => setAttachmentSecret(undefined)}>
                        Dismiss credential
                      </button>
                    </div>
                  </Show>
                  <For each={snapshot()!.attachments}>
                    {(item) => (
                      <>
                        <div class="profile-row">
                          <div>
                            <strong>
                              {item.name} <span class="tag">EXTERNAL</span>
                            </strong>
                            <small>
                              {item.id} · {item.status} ·{" "}
                              {item.allow.join(", ")}
                            </small>
                          </div>
                          <button
                            class="small"
                            disabled={item.status === "revoked"}
                            onClick={() =>
                              act(() =>
                                api(`attachments/${item.id}`, "DELETE", {}),
                              )
                            }
                          >
                            Revoke access
                          </button>
                        </div>
                        <details>
                          <summary>
                            Operations admitted through this attachment
                          </summary>
                          <Show
                            when={snapshot()!.operations.some(
                              (op) => op.attachment_id === item.id,
                            )}
                            fallback={<p>No Entryplug calls observed.</p>}
                          >
                            <For
                              each={snapshot()!.operations.filter(
                                (op) => op.attachment_id === item.id,
                              )}
                            >
                              {(op) => (
                                <div class="profile-row">
                                  <button
                                    class="text-button"
                                    onClick={() => inspect(op)}
                                  >
                                    {op.capability} · {op.lifecycle} · effect{" "}
                                    {op.effect_state} ↗
                                  </button>
                                  <button
                                    class="small"
                                    disabled={
                                      busy() ||
                                      ![
                                        "accepted",
                                        "running",
                                        "canceling",
                                      ].includes(op.lifecycle)
                                    }
                                    onClick={() =>
                                      act(() =>
                                        api(
                                          `operations/${op.id}/cancel`,
                                          "POST",
                                          {},
                                        ),
                                      )
                                    }
                                  >
                                    Request cancellation
                                  </button>
                                </div>
                              )}
                            </For>
                          </Show>
                        </details>
                      </>
                    )}
                  </For>
                </section>
              </div>
            </Show>
          </Show>
        </main>
        <footer class="footer">
          <span>◇ ENTRYPLUG</span>
          <span>Reasoning at the task level. Control stays with the body.</span>
          <span>LOCAL / V1</span>
        </footer>
      </div>
      <dialog
        ref={dialog}
        onClick={(e) => {
          if (e.target === dialog) dialog.close();
        }}
      >
        <div class="dialog-heading">
          <h2>Inspect evidence & state</h2>
          <button onClick={() => dialog.close()} autofocus>
            Close ✕
          </button>
        </div>
        <Show when={detail()}>
          <EvidenceImages value={detail()} />
        </Show>
        <pre>{pretty(detail())}</pre>
      </dialog>
    </div>
  );
}

function EvidenceImages(props: { value: unknown }) {
  const images = () => {
    const output: { data: string; mime: string }[] = [];
    function visit(value: any) {
      if (!value || typeof value !== "object" || output.length >= 4) return;
      if (
        value.type === "image" &&
        typeof value.data === "string" &&
        ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(
          value.mimeType ?? value.mime_type,
        )
      )
        output.push({
          data: value.data,
          mime: value.mimeType ?? value.mime_type,
        });
      else Object.values(value).forEach(visit);
    }
    visit(props.value);
    return output;
  };
  return (
    <For each={images()}>
      {(image) => (
        <img
          style={{ "max-width": "100%" }}
          src={`data:${image.mime};base64,${image.data}`}
          alt="Selected source evidence; inspect its origin and timestamp below"
        />
      )}
    </For>
  );
}

function Metric(props: { label: string; value: number; note: string }) {
  return (
    <div class="metric">
      <span class="eyebrow">{props.label}</span>
      <strong>{props.value.toString().padStart(2, "0")}</strong>
      <small>{props.note}</small>
    </div>
  );
}
function MissionCard(props: { run: RecordData; open: () => void }) {
  return (
    <button class="mission-card" onClick={props.open}>
      <div class="card-top">
        <span class="eyebrow">
          {props.run.body_id.startsWith("demo.")
            ? "SIMULATED BODY"
            : "CONNECTED BODY"}
        </span>
        <span>↗</span>
      </div>
      <h3>{props.run.name}</h3>
      <span
        class="status"
        classList={{ fault: props.run.health === "blocked" }}
      >
        <span class="dot live" />
        {label(props.run.lifecycle)} · {label(props.run.activity)}
      </span>
      <p>
        {props.run.reason_code
          ? label(props.run.reason_code)
          : (props.run.last_result ?? "Waiting for an entrance event")}
      </p>
      <div class="card-bottom">
        <span>
          {props.run.turn_count} turns · {props.run.model_calls} model calls
        </span>
        <span>r{props.run.revision}</span>
      </div>
    </button>
  );
}

render(() => <App />, document.getElementById("root")!);
