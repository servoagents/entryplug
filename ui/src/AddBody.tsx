import { createSignal, For, Show } from "solid-js";
import { api } from "./api";
import type { RecordData } from "./api";
import ProtocolIcon from "./ProtocolIcon";

const protocols = [
  ["simulation", "Simulation", "Local · no hardware"],
  ["homeassistant", "Home Assistant", "Light state · WebSocket"],
  ["mcp", "MCP", "Tools from a server"],
  ["a2a", "A2A", "Another Entryplug body"],
  ["ros2", "ROS 2", "Through an MCP bridge"],
  ["zenoh", "Zenoh", "Through an MCP bridge"],
  ["mqtt", "MQTT", "Through an MCP bridge"],
];
export default function AddBody(props: {
  added: (body?: string) => Promise<void>;
  connections: RecordData[];
  refresh: () => Promise<void>;
}) {
  const [kind, setKind] = createSignal("simulation");
  const [simulation, setSimulation] = createSignal("camera");
  const [name, setName] = createSignal("My camera");
  const [url, setUrl] = createSignal("");
  const [entity, setEntity] = createSignal("light.entrance");
  const [tools, setTools] = createSignal("");
  const [env, setEnv] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal("");
  const [success, setSuccess] = createSignal("");
  const bridge = () => ["ros2", "zenoh", "mqtt"].includes(kind());
  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      if (kind() === "simulation") {
        const body = await api("simulations", "POST", {
          kind: simulation(),
          name: name(),
        });
        await props.added(body.id);
      } else {
        const payload: RecordData = {
          kind: bridge() ? "mcp" : kind(),
          name: name(),
          url: url(),
        };
        if (kind() === "homeassistant") payload.entity_id = entity();
        else
          payload.tools = tools()
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
        if (env()) payload.token_env = env();
        if (bridge()) payload.source_protocol = kind();
        await api("connections", "POST", payload);
        await props.refresh();
        setSuccess(
          "Connection saved. Its status below shows whether the body has been verified.",
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div class="add-body-layout">
      <div
        class="protocol-picker"
        role="group"
        aria-label="Body connection type"
      >
        <For each={protocols}>
          {([id, title, note]) => (
            <button
              aria-pressed={kind() === id}
              classList={{ selected: kind() === id }}
              onClick={() => {
                setKind(id);
                setName(id === "simulation" ? "My camera" : title + " body");
                setUrl("");
                setEnv("");
                setError("");
              }}
            >
              <ProtocolIcon kind={id} />
              <span>
                <strong>{title}</strong>
                <small>{note}</small>
              </span>
              <span class="picker-dot" />
            </button>
          )}
        </For>
      </div>
      <section class="panel body-form">
        <span class="eyebrow">
          {kind() === "simulation" ? "LOCAL SIMULATION" : "BODY CONNECTION"}
        </span>
        <h2>
          {kind() === "simulation"
            ? "Add a simulated body"
            : bridge()
              ? `${protocols.find((p) => p[0] === kind())![1]} via MCP`
              : `Connect ${protocols.find((p) => p[0] === kind())![1]}`}
        </h2>
        <Show when={kind() === "simulation"}>
          <p>
            Each body has its own state and operations. No device or account is
            needed.
          </p>
        </Show>
        <Show when={bridge()}>
          <div class="setup-notice">
            <strong>An MCP bridge is required</strong>
            <p>
              Connect a bridge that exposes your{" "}
              {protocols.find((p) => p[0] === kind())![1]} device as read-only
              tools. Enter its HTTP MCP endpoint below. Direct ROS 2, Zenoh and
              MQTT discovery is not available in this console.
            </p>
          </div>
        </Show>
        <Show when={kind() === "a2a"}>
          <p>
            Use an Entryplug Agent Card URL with read-only capabilities.
            Delegation is limited to one hop.
          </p>
        </Show>
        <Show when={error()}>
          <p class="error" role="alert">
            {error()}
          </p>
        </Show>
        <Show when={success()}>
          <p class="connection-ok" role="status">
            {success()}
          </p>
        </Show>
        <form onSubmit={submit}>
          <label>
            Body name
            <input
              required
              value={name()}
              onInput={(e) => setName(e.currentTarget.value)}
            />
          </label>
          <Show
            when={kind() === "simulation"}
            fallback={
              <>
                <label>
                  {kind() === "homeassistant"
                    ? "Home Assistant WebSocket URL"
                    : kind() === "a2a"
                      ? "Agent Card URL"
                      : "MCP server URL"}
                  <input
                    required
                    type="url"
                    value={url()}
                    placeholder={
                      kind() === "homeassistant"
                        ? "ws://127.0.0.1:8123/api/websocket"
                        : "http://127.0.0.1:8000/mcp"
                    }
                    onInput={(e) => setUrl(e.currentTarget.value)}
                  />
                </label>
                <Show
                  when={kind() === "homeassistant"}
                  fallback={
                    <label>
                      Read-only capability names
                      <input
                        required
                        value={tools()}
                        placeholder="camera.snapshot, robot.observe"
                        onInput={(e) => setTools(e.currentTarget.value)}
                      />
                    </label>
                  }
                >
                  <label>
                    Light entity
                    <input
                      required
                      value={entity()}
                      onInput={(e) => setEntity(e.currentTarget.value)}
                    />
                  </label>
                </Show>
                <label>
                  Credential environment variable
                  <input
                    value={env()}
                    placeholder={
                      kind() === "homeassistant"
                        ? "ENTRYPLUG_HA_TOKEN"
                        : kind() === "a2a"
                          ? "ENTRYPLUG_A2A_TOKEN"
                          : "ENTRYPLUG_MCP_TOKEN"
                    }
                    onInput={(e) => setEnv(e.currentTarget.value)}
                  />
                </label>
                <p>
                  Enter the variable name, not the secret. Set its value in the
                  environment that starts Entryplug.
                </p>
              </>
            }
          >
            <div
              class="body-kind-picker"
              role="group"
              aria-label="Simulation body type"
            >
              <For
                each={[
                  ["camera", "Camera"],
                  ["rover", "Rover"],
                  ["room", "Smart room"],
                ]}
              >
                {([id, title]) => (
                  <button
                    type="button"
                    aria-pressed={simulation() === id}
                    classList={{ selected: simulation() === id }}
                    onClick={() => {
                      setSimulation(id);
                      setName("My " + title.toLowerCase());
                    }}
                  >
                    <ProtocolIcon kind={id} large />
                    <strong>{title}</strong>
                  </button>
                )}
              </For>
            </div>
          </Show>
          <button class="primary" disabled={busy()}>
            {busy()
              ? "Adding…"
              : kind() === "simulation"
                ? "Add simulated body"
                : "Connect body"}
          </button>
        </form>
        <Show when={props.connections.length}>
          <h3>Saved connections</h3>
          <For each={props.connections}>
            {(c) => (
              <div class="profile-row">
                <ProtocolIcon kind={c.source_protocol || c.kind} />
                <div>
                  <strong>{c.name}</strong>
                  <small>
                    {c.status}{" "}
                    {c.reason_code
                      ? `· ${c.reason_code.replaceAll("_", " ")}`
                      : ""}
                  </small>
                </div>
              </div>
            )}
          </For>
        </Show>
      </section>
    </div>
  );
}
