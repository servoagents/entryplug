import { Match, Switch } from "solid-js";

export default function ProtocolIcon(props: { kind: string; large?: boolean }) {
  return (
    <span
      class={`protocol-icon protocol-${props.kind}`}
      classList={{ large: props.large }}
    >
      <svg
        viewBox="0 0 32 32"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <Switch
          fallback={
            <>
              <circle cx="16" cy="16" r="10" />
              <path d="M11 16h10m-5-5v10" />
            </>
          }
        >
          <Match when={props.kind === "camera"}>
            <rect x="4" y="8" width="24" height="17" rx="5" />
            <circle cx="16" cy="16" r="5" />
            <path d="m9 8 2-3h10l2 3" />
          </Match>
          <Match when={props.kind === "rover"}>
            <rect x="7" y="10" width="18" height="13" rx="5" />
            <path d="M16 10V5m-6 10h2m8 0h2M5 15v7m22-7v7" />
            <circle cx="11" cy="25" r="2" />
            <circle cx="22" cy="25" r="2" />
          </Match>
          <Match when={props.kind === "room" || props.kind === "homeassistant"}>
            <path d="m3 15 13-11 13 11M7 12v16h18V12M13 28V18h6v10" />
          </Match>
          <Match when={props.kind === "ros2"}>
            <circle cx="8" cy="8" r="2" />
            <circle cx="16" cy="8" r="2" />
            <circle cx="24" cy="8" r="2" />
            <circle cx="8" cy="16" r="2" />
            <circle cx="16" cy="16" r="2" />
            <circle cx="24" cy="16" r="2" />
            <circle cx="8" cy="24" r="2" />
            <circle cx="16" cy="24" r="2" />
            <circle cx="24" cy="24" r="2" />
          </Match>
          <Match when={props.kind === "zenoh"}>
            <path d="M6 7h20L6 25h20M10 16h12" />
          </Match>
          <Match when={props.kind === "mqtt"}>
            <circle cx="7" cy="25" r="2" />
            <path d="M6 16a10 10 0 0 1 10 10M6 7a19 19 0 0 1 19 19" />
            <path d="M21 5h6v6" />
          </Match>
          <Match when={props.kind === "mcp"}>
            <path d="m5 18 12-12a5 5 0 0 1 7 7L12 25m-4-3 12-12M17 25l5 5" />
          </Match>
          <Match when={props.kind === "a2a"}>
            <circle cx="7" cy="16" r="4" />
            <circle cx="25" cy="16" r="4" />
            <path d="M11 13h10m-3-3 3 3-3 3M21 20H11m3-3-3 3 3 3" />
          </Match>
          <Match when={props.kind === "simulation" || props.kind === "agent"}>
            <rect x="5" y="5" width="22" height="22" rx="7" />
            <path d="m13 11 8 5-8 5z" />
          </Match>
        </Switch>
      </svg>
    </span>
  );
}
