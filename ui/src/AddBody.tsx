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
  ["zenoh", "Zenoh", "Included read-only bridge"],
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
                setTools("");
                setSuccess("");
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
          <Show
            when={kind() === "zenoh"}
            fallback={
              <div class="setup-notice">
                <strong>External bridge required</strong>
                <p>
                  Entryplug does not yet include a{" "}
                  {kind() === "ros2" ? "ROS 2" : "MQTT"} bridge for this form.
                  Use this connection only if you already run an HTTP MCP server
                  that exposes your device's observations as read-only tools. A
                  native topic or broker address cannot go in the MCP URL field.
                </p>
                <p>
                  The server must advertise the selected tools with readOnlyHint
                  enabled. Ask your device integration's maintainer for its MCP
                  endpoint and exact tool names.
                </p>
              </div>
            }
          >
            <div class="setup-notice bridge-guide">
              <strong>Run the included Zenoh bridge</strong>
              <p>
                This small process reads selected Zenoh publications and exposes
                them to Entryplug. Run it on the same computer as the Entryplug
                service. It cannot send device commands.
              </p>
              <ol>
                <li>
                  In your Entryplug Python environment, install the optional
                  bridge support:
                  <pre>
                    python -m pip install
                    './dist/entryplug-0.0.1-py3-none-any.whl[ui,mcp,zenoh]'
                  </pre>
                  Restart Entryplug after installing these extras.
                </li>
                <li>
                  Get the router or peer endpoint and the exact observation key
                  from your device configuration. Replace the example endpoint
                  and key, then leave this command running in another terminal:
                  <pre>
                    python -m entryplug bridge zenoh --connect
                    tcp/127.0.0.1:7447 --key demo/temperature
                  </pre>
                  Repeat <code>--key</code> for more observations. Wildcards are
                  not accepted.
                </li>
                <li>
                  Use the local bridge settings below, then choose{" "}
                  <strong>Connect body</strong>. No credential is needed for
                  this loopback-only bridge.
                </li>
              </ol>
              <details>
                <summary>No device yet? Try a simulated publisher</summary>
                <p>
                  Run this in a separate terminal before the bridge command
                  above. It publishes labelled simulated temperatures once per
                  second using real Zenoh transport.
                </p>
                <pre>python -m entryplug bridge zenoh-demo</pre>
                <p>
                  Ctrl-C stops the publisher. After five seconds without a new
                  sample, reads fail as stale.
                </p>
              </details>
              <p>
                Supports UTF-8 text and JSON, up to 16 KiB per sample. Reads
                report receive age; sensor capture age is unknown. No camera
                decoding or automatic device discovery.
              </p>
              <button
                type="button"
                onClick={() => {
                  setUrl("http://127.0.0.1:8766/mcp");
                  setTools("zenoh_observe");
                  setEnv("");
                }}
              >
                Use local bridge settings
              </button>
            </div>
          </Show>
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
