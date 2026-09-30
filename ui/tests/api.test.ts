import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, authenticate, watchEvents } from "../src/api";

afterEach(() => vi.unstubAllGlobals());

describe("local API boundary", () => {
  it("exchanges and removes the one-use fragment, then sends CSRF and revision headers", async () => {
    const replaceState = vi.fn();
    vi.stubGlobal("location", { hash: "#bootstrap=one-use", pathname: "/" });
    vi.stubGlobal("history", { replaceState });
    const fetch = vi.fn(async (_path, init) => {
      expect(replaceState).toHaveBeenCalledWith(null, "", "/");
      return new Response(
        JSON.stringify({
          csrf: "csrf-session",
          api_version: "1",
          build_id: "mission-v1",
        }),
      );
    });
    vi.stubGlobal("fetch", fetch);
    await authenticate();
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      token: "one-use",
    });
    await api("missions/m1", "PATCH", { definition: {} }, 7);
    expect(fetch.mock.calls[1][1].headers).toMatchObject({
      "If-Match": "7",
      "X-Entryplug-CSRF": "csrf-session",
    });
    expect(fetch.mock.calls[1][1].headers["Idempotency-Key"]).not.toBe(
      fetch.mock.calls[0][1].headers["Idempotency-Key"],
    );
  });

  it("preserves a typed stale-revision error instead of treating acceptance as success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              code: "revision_conflict",
              message: "Reload revision",
            }),
            { status: 412 },
          ),
      ),
    );
    await expect(api("missions/m1", "PATCH", {}, 1)).rejects.toMatchObject({
      code: "revision_conflict",
      status: 412,
    });
    await expect(api("missions/m1")).rejects.toBeInstanceOf(ApiError);
  });

  it("closes an expired event stream and asks the caller to resnapshot", () => {
    const callbacks: Record<string, (event: { data: string }) => void> = {};
    const close = vi.fn(),
      resnapshot = vi.fn(),
      received = vi.fn();
    vi.stubGlobal(
      "EventSource",
      class {
        constructor(public url: string) {
          expect(url).toBe("/v1/events?after=42");
        }
        addEventListener(name: string, callback: (typeof callbacks)[string]) {
          callbacks[name] = callback;
        }
        close = close;
      },
    );
    const dispose = watchEvents(42, received, resnapshot);
    callbacks.entryplug({ data: '{"seq":43,"type":"alert.created"}' });
    expect(received).toHaveBeenCalledWith({ seq: 43, type: "alert.created" });
    callbacks.resync({ data: '{"code":"replay_expired"}' });
    expect(close).toHaveBeenCalledOnce();
    expect(resnapshot).toHaveBeenCalledOnce();
    dispose();
  });
});
