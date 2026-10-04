import { createSignal, For, Show } from "solid-js";
import type { RecordData } from "./api";
import SimulationView from "./SimulationView";

const guides: Record<
  string,
  { goal: string; expected: string; purpose: string }
> = {
  rover: {
    goal: "Patrol a route and wait when an obstacle appears.",
    expected:
      "Move to the aisle, hold at the obstacle, continue to the station, then return to the dock.",
    purpose:
      "Practice observe → act → verify, and see a controller refuse movement on a blocked route.",
  },
  room: {
    goal: "Keep the light on only while the room is occupied.",
    expected:
      "Start empty, turn the light on after someone arrives, and switch it off when they leave.",
    purpose:
      "Connect a sensor to an actuator and verify the resulting device state.",
  },
  entries: {
    goal: "Record each new entry once, including a return visit.",
    expected:
      "Three entry alerts with camera evidence. Continued presence should not create another alert.",
    purpose:
      "Distinguish a new event from repeated observations. Events are scripted; the recording is illustrative.",
  },
  outage: {
    goal: "Stop trusting a camera when its connection is lost.",
    expected:
      "Record the first entry, block during the outage, then resume after the camera reconnects.",
    purpose:
      "See the difference between missing information and evidence that nobody is there.",
  },
  stale: {
    goal: "Reject an old observation instead of reporting a new entry.",
    expected:
      "Record fresh entries, reject the stale frame, and recover when fresh observations return.",
    purpose:
      "Test observation age and mission recovery without using a real camera.",
  },
};
const events: Record<string, string> = {
  idle: "Empty scene",
  enter_a: "Person A enters",
  presence_a: "Person A remains",
  enter_b: "Person B enters",
  exit_a: "Person A leaves",
  lost: "Source disconnects",
  recover: "Source reconnects",
  stale: "Old observation arrives",
  tick: "Patrol observation",
  obstacle: "Obstacle appears",
  clear: "Route clears",
  leave: "Room becomes empty",
  arrive: "Someone arrives",
};

export default function SimulationWorkbench(props: {
  simulation: RecordData;
  run?: RecordData;
  agentLabel: string;
  native: boolean;
  busy: boolean;
  liveText?: string;
  profiles: RecordData[];
  start: (profile: string) => void;
  manage: (action: "pause" | "resume" | "stop" | "replay") => void;
  control: (id: string, action: string, fields?: RecordData) => void;
  inspect: () => void;
  connect: () => void;
}) {
  const [profile, setProfile] = createSignal("");
  const s = () => props.simulation;
  const guide = () => guides[s().kind === "camera" ? s().scenario : s().kind];
  const ended = () => s().remaining_events === 0 && !s().playing;
  const occupied = () =>
    !!props.run?.active_turn_id || !!props.run?.pending?.length;
  return (
    <>
      <section class="panel experiment-brief">
        <div>
          <span class="eyebrow">YOUR TASK</span>
          <h2>{guide().goal}</h2>
          <p>{guide().purpose}</p>
          <p>
            <strong>Expected behavior: </strong>
            {guide().expected}
          </p>
        </div>
        <Show when={s().kind === "camera"}>
          <label>
            Scenario
            <select
              value={s().scenario}
              disabled={props.busy || !!props.run}
              onChange={(e) =>
                props.control(s().id, "scenario", {
                  scenario: e.currentTarget.value,
                })
              }
            >
              <option value="entries">Entries and return visits</option>
              <option value="outage">Camera disconnects</option>
              <option value="stale">Old frame returned</option>
            </select>
            <Show when={props.run}>
              <small>Stop the demo to change scenarios.</small>
            </Show>
          </label>
        </Show>
      </section>
      <div class="experiment-controls panel">
        <Show
          when={!props.run}
          fallback={
            <>
              <div class="experiment-status" role="status">
                <strong>{props.agentLabel}</strong>
                <span>
                  {s().playing
                    ? "World playing"
                    : ended()
                      ? "Timeline complete"
                      : "World paused"}
                </span>
                <span>
                  {props.run?.health === "blocked"
                    ? "Agent blocked"
                    : props.run?.lifecycle === "paused"
                      ? "Agent paused"
                      : occupied()
                        ? "Agent working"
                        : "Agent waiting for an event"}
                </span>
              </div>
              <div class="actions">
                <Show when={props.run?.health !== "blocked"}>
                  <button
                    class="primary"
                    disabled={
                      props.busy ||
                      ((ended() || props.run?.lifecycle === "paused") &&
                        occupied())
                    }
                    onClick={() =>
                      props.manage(
                        ended()
                          ? "replay"
                          : props.run?.lifecycle === "paused"
                            ? "resume"
                            : "pause",
                      )
                    }
                  >
                    {ended()
                      ? "Replay demo"
                      : props.run?.lifecycle === "paused"
                        ? "Resume demo"
                        : "Pause demo"}
                  </button>
                </Show>
                <button
                  disabled={
                    props.busy ||
                    occupied() ||
                    props.run?.lifecycle !== "active" ||
                    s().remaining_events === 0
                  }
                  onClick={() => props.control(s().id, "step")}
                >
                  Next event
                </button>
                <button
                  disabled={props.busy}
                  onClick={() => props.manage("stop")}
                >
                  Stop demo
                </button>
                <button onClick={props.inspect}>
                  View mission &amp; evidence
                </button>
              </div>
            </>
          }
        >
          <label>
            Controller
            <select
              value={profile()}
              onChange={(e) => setProfile(e.currentTarget.value)}
            >
              <option value="">Built-in demo · no model, no account</option>
              <For each={props.profiles.filter((p) => p.status === "ready")}>
                {(p) => (
                  <option value={p.id}>
                    {p.provider} · {p.model}
                  </option>
                )}
              </For>
            </select>
          </label>
          <div class="actions">
            <button
              class="primary"
              disabled={props.busy}
              onClick={() => props.start(profile())}
            >
              {profile() ? "Start with model" : "Start demo"}
            </button>
            <button onClick={props.connect}>Connect a real model</button>
          </div>
          <p>
            {profile()
              ? "Sends one event at a time. Wait for the model, then choose Next event. Provider charges may apply to hosted profiles."
              : "Starts the world and its built-in controller together. The controller follows a fixed policy and makes no model calls."}
          </p>
        </Show>
        <Show when={props.native && props.run}>
          <p>
            Model mode uses one event at a time. Choose Next event after the
            agent finishes.
          </p>
        </Show>
        <Show when={props.run?.health === "blocked"}>
          <p class="notice">
            Waiting for recovery: {props.run?.reason_code}. Inspect the mission,
            or reconnect the source under World controls.
          </p>
        </Show>
        <Show when={ended() && props.run}>
          <p>
            The scripted events have finished
            {occupied()
              ? "; the agent is finishing its turn"
              : ". Review the result below"}
            . The mission stays available for manual events until you stop it.
          </p>
        </Show>
      </div>
      <div class="simulation-detail">
        <SimulationView
          simulation={s()}
          control={props.control}
          busy={props.busy}
        />
        <aside class="panel simulation-guide">
          <h2>What happens next</h2>
          <ol class="scenario-timeline">
            <For each={s().timeline ?? []}>
              {(cue) => (
                <li classList={{ emitted: cue.emitted }}>
                  <span>{cue.emitted ? "✓" : `${cue.at_s}s`}</span>{" "}
                  {events[cue.event] ?? cue.event}
                </li>
              )}
            </For>
          </ol>
          <p class="timeline-key">
            ✓ means the world event was sent. Check the agent result to see its
            response.
          </p>
          <Show when={s().last_event}>
            <p>
              <strong>Latest world event: </strong>
              {events[s().last_event] ?? s().last_event}
            </p>
          </Show>
          <h3>Agent response</h3>
          <p class="agent-response" aria-live="polite">
            {props.liveText ||
              props.run?.last_result ||
              (props.run
                ? "Waiting for the first response…"
                : "Start demo to see observations, decisions, and tool results here.")}
          </p>
          <Show when={props.run}>
            <small>
              {props.run?.turn_count} finished turns · {props.run?.tool_calls}{" "}
              tool calls · {props.run?.model_calls} model calls
            </small>
          </Show>
        </aside>
      </div>
    </>
  );
}
