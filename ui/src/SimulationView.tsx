import { createEffect, createSignal, For, onCleanup, Show } from "solid-js";
import type { RecordData } from "./api";
import ProtocolIcon from "./ProtocolIcon";

export default function SimulationView(props: {
  simulation: RecordData;
  control: (id: string, action: string, fields?: RecordData) => void;
  busy?: boolean;
}) {
  let video: HTMLVideoElement | undefined;
  const [now, setNow] = createSignal(Date.now());
  const [videoError, setVideoError] = createSignal(false);
  const s = () => props.simulation;
  const position = () =>
    Math.min(
      s().duration_s,
      s().position_s +
        (s().playing
          ? Math.max(0, now() - Date.parse(s().sampled_at)) / 1000
          : 0),
    );
  createEffect(() => {
    if (!s().playing) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 250);
    onCleanup(() => clearInterval(timer));
  });
  function syncVideo() {
    if (!video || !video.readyState) return;
    const at = position();
    if (Math.abs(video.currentTime - at) > 0.65) video.currentTime = at;
    if (s().playing && s().available)
      void video.play().catch(() => setVideoError(true));
    else video.pause();
  }
  createEffect(() => {
    s();
    syncVideo();
  });
  const command = (action: string, fields: RecordData = {}) =>
    props.control(s().id, action, fields);
  return (
    <section class={`simulation-view scene-${s().kind}`}>
      <div class="scene-heading">
        <span class="sim-badge">SIMULATION</span>
        <span>{s().name}</span>
        <span class="scene-clock">
          {position().toFixed(1)} / {s().duration_s.toFixed(1)} s
        </span>
      </div>
      <div class="scene-stage" classList={{ "source-offline": !s().available }}>
        <Show when={s().kind === "camera"}>
          <video
            ref={video}
            src={s().recording}
            preload="auto"
            muted
            playsinline
            onLoadedData={syncVideo}
            onError={() => setVideoError(true)}
            aria-label="Recorded street scene used for the camera simulation"
          />
          <div class="recording-label">
            RECORDED SAMPLE <span>•</span> SCRIPTED EVENTS
          </div>
          <Show when={videoError()}>
            <div class="scene-message">
              Recording could not load. Reload the console to retry.
            </div>
          </Show>
        </Show>
        <Show when={s().kind === "rover"}>
          <svg
            viewBox="0 0 600 330"
            class="world-map"
            role="img"
            aria-label={`Simulated rover at ${s().state.waypoint}; battery ${s().state.battery} percent`}
          >
            <path
              class="floor-lines"
              d="M0 55h600M0 110h600M0 165h600M0 220h600M0 275h600M60 0v330M120 0v330M180 0v330M240 0v330M300 0v330M360 0v330M420 0v330M480 0v330M540 0v330"
            />
            <rect
              class="shelf"
              x="180"
              y="45"
              width="145"
              height="58"
              rx="10"
            />
            <rect
              class="shelf"
              x="180"
              y="228"
              width="145"
              height="58"
              rx="10"
            />
            <path class="route-line" d="M85 170H300L485 120" />
            <circle class="waypoint" cx="85" cy="170" r="25" />
            <circle class="waypoint" cx="300" cy="170" r="25" />
            <circle class="waypoint" cx="485" cy="120" r="25" />
            <text x="85" y="225">
              DOCK
            </text>
            <text x="300" y="210">
              AISLE
            </text>
            <text x="485" y="175">
              STATION
            </text>
            <g
              class="rover-sprite"
              style={{
                transform: `translate(${({ dock: 85, aisle: 300, station: 485 } as Record<string, number>)[s().state.waypoint]}px, ${({ dock: 170, aisle: 170, station: 120 } as Record<string, number>)[s().state.waypoint]}px)`,
              }}
            >
              <rect x="-23" y="-19" width="46" height="38" rx="12" />
              <path d="M-12-23v-7M12-23v-7M-13 0h5m16 0h5" />
              <circle cx="0" cy="-32" r="3" />
            </g>
            <Show when={s().state.obstacle}>
              <g class="map-obstacle">
                <path d="m373 146 22 39h-44z" />
                <text x="373" y="177">
                  !
                </text>
              </g>
            </Show>
          </svg>
          <div class="scene-readouts">
            <span>
              Battery <b>{s().state.battery}%</b>
            </span>
            <span>
              Obstacle <b>{s().state.obstacle ? "Detected" : "Clear"}</b>
            </span>
          </div>
        </Show>
        <Show when={s().kind === "room"}>
          <svg
            viewBox="0 0 600 330"
            class="room-map"
            classList={{ illuminated: s().state.light }}
            role="img"
            aria-label={`Simulated room: ${s().state.occupied ? "occupied" : "empty"}, light ${s().state.light ? "on" : "off"}`}
          >
            <rect
              class="room-floor"
              x="70"
              y="30"
              width="460"
              height="265"
              rx="20"
            />
            <path class="room-door" d="M265 295v-75a75 75 0 0 1 75 75" />
            <rect
              class="room-table"
              x="135"
              y="100"
              width="130"
              height="80"
              rx="20"
            />
            <rect
              class="room-sofa"
              x="375"
              y="75"
              width="105"
              height="145"
              rx="16"
            />
            <circle class="lamp-glow" cx="300" cy="105" r="85" />
            <circle class="lamp" cx="300" cy="105" r="16" />
            <Show when={s().state.occupied}>
              <g class="person-sprite">
                <circle cx="312" cy="208" r="14" />
                <path d="M312 225v30m-20-17 20-13 20 13m-20 17-14 22m14-22 14 22" />
              </g>
            </Show>
          </svg>
          <div class="scene-readouts">
            <span>
              Occupancy{" "}
              <b>{s().state.occupied ? "Someone is here" : "Empty"}</b>
            </span>
            <span>
              Light <b>{s().state.light ? "On" : "Off"}</b>
            </span>
          </div>
        </Show>
        <Show when={!s().available}>
          <div class="scene-message">
            <b>Source disconnected</b>
            <span>Last view retained. Current state is unknown.</span>
          </div>
        </Show>
      </div>
      <div class="playback-track" aria-label="Scenario progress">
        <span style={{ width: `${(position() / s().duration_s) * 100}%` }} />
      </div>
      <div class="playback-controls">
        <button
          class="primary"
          disabled={props.busy}
          onClick={() => command(s().playing ? "pause" : "play")}
        >
          {s().playing ? "Ⅱ Pause scenario" : "▶ Play scenario"}
        </button>
        <button disabled={props.busy} onClick={() => command("step")}>
          Next event
        </button>
        <button disabled={props.busy} onClick={() => command("reset")}>
          ↺ Reset
        </button>
        <Show when={s().kind === "camera"}>
          <label>
            Scenario
            <select
              value={s().scenario}
              disabled={props.busy}
              onChange={(e) =>
                command("scenario", { scenario: e.currentTarget.value })
              }
            >
              <option value="entries">People entering & returning</option>
              <option value="outage">Camera disconnects</option>
              <option value="stale">Old frame returned</option>
            </select>
          </label>
        </Show>
      </div>
      <div class="fault-controls">
        <span>Try an event</span>
        <For
          each={
            s().kind === "camera"
              ? [
                  ["enter_a", "Person enters"],
                  ["exit_a", "Person leaves"],
                  ["lost", "Disconnect"],
                  ["recover", "Reconnect"],
                ]
              : s().kind === "rover"
                ? [
                    ["tick", "Patrol step"],
                    ["obstacle", "Add obstacle"],
                    ["clear", "Clear route"],
                    ["lost", "Disconnect"],
                    ["recover", "Reconnect"],
                  ]
                : [
                    ["arrive", "Someone arrives"],
                    ["leave", "Everyone leaves"],
                    ["lost", "Disconnect"],
                    ["recover", "Reconnect"],
                  ]
          }
        >
          {([event, name]) => (
            <button
              class="small"
              disabled={props.busy}
              onClick={() => command("inject", { event })}
            >
              {name}
            </button>
          )}
        </For>
      </div>
      <Show when={s().kind === "camera"}>
        <p class="media-credit">
          Recorded footage:{" "}
          <a
            href="https://commons.wikimedia.org/wiki/File:Big_City_Life.webm"
            target="_blank"
            rel="noreferrer"
          >
            Big City Life · Coverr · CC0
          </a>
          . The scenario generates events independently of the recording; it
          does not run a person detector.
        </p>
      </Show>
      <p class="scene-note">
        <ProtocolIcon kind="simulation" />
        The service runs this scenario. Closing this page does not stop playback
        or its missions.
      </p>
    </section>
  );
}
