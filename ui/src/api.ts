import type { components } from "./generated";

export type Definition = components["schemas"]["MissionDefinition"];
export type Lifecycle = components["schemas"]["MissionLifecycle"];
export type Activity = components["schemas"]["Activity"];
export type Health = components["schemas"]["Health"];
export type RecordData = Record<string, any>;
type JsonTypes<T> = unknown extends T
  ? any
  : T extends (infer I)[]
    ? JsonTypes<I>[]
    : T extends object
      ? { [P in keyof T]: JsonTypes<T[P]> }
      : T;
export type Snapshot = JsonTypes<components["schemas"]["Snapshot"]>;

let csrf = "";
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T = RecordData>(
  path: string,
  method = "GET",
  data?: unknown,
  revision?: number,
): Promise<T> {
  const response = await fetch(`/v1/${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-Entryplug-CSRF": csrf,
      "Idempotency-Key": crypto.randomUUID(),
      ...(revision === undefined ? {} : { "If-Match": String(revision) }),
    },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok)
    throw new ApiError(body.code, body.message, response.status);
  return body;
}
export async function authenticate(): Promise<void> {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("bootstrap");
  if (token) history.replaceState(null, "", location.pathname);
  const session = token
    ? await api("session/exchange", "POST", { token })
    : await api("session");
  if (session.build_id !== "mission-v1" || session.api_version !== "1")
    throw new Error("Console and service versions differ. Reload this page.");
  csrf = session.csrf;
}

export function watchEvents(
  cursor: number,
  onEvent: (event: RecordData) => void,
  onDisconnect: () => void,
): () => void {
  const stream = new EventSource(`/v1/events?after=${cursor}`, {
    withCredentials: true,
  });
  stream.addEventListener("entryplug", (event) =>
    onEvent(JSON.parse((event as MessageEvent).data)),
  );
  stream.addEventListener("activity", (event) =>
    onEvent(JSON.parse((event as MessageEvent).data)),
  );
  stream.addEventListener("resync", () => {
    stream.close();
    onDisconnect();
  });
  stream.onerror = () => {
    stream.close();
    onDisconnect();
  };
  return () => stream.close();
}

export const pretty = (value: unknown) => JSON.stringify(value, null, 2);
export const label = (value: string) => value.replaceAll("_", " ");
export const time = (value?: string) =>
  value
    ? new Date(value).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "—";
