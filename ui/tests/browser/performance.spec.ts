import { expect, test } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

test("measure loopback state visibility and a fixed 1000-node topology workload", async ({
  page,
  request,
  browser,
}) => {
  test.setTimeout(60000);
  const token = readFileSync(
    "/tmp/entryplug-playwright/config/client.token",
    "utf8",
  ).trim();
  const headers = () => ({
    Authorization: `Bearer ${token}`,
    "Idempotency-Key": crypto.randomUUID(),
  });
  const mission = await (
    await request.post("/v1/missions", {
      headers: headers(),
      data: {
        name: "Latency fixture",
        instructions: "Wait for labelled entries",
      },
    })
  ).json();
  const run = await (
    await request.post(`/v1/missions/${mission.id}/runs`, {
      headers: headers(),
      data: {},
    })
  ).json();
  const bootstrap = await (
    await request.post("/v1/session/bootstrap", { headers: headers() })
  ).json();
  await page.goto(`/#bootstrap=${bootstrap.bootstrap}`);
  await expect(
    page.getByText("Service connected", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: /Missions/ }).click();
  await page.getByLabel("Execution", { exact: true }).selectOption(run.id);
  const state = await (
    await request.get("/v1/snapshot", { headers: headers() })
  ).json();
  await page.evaluate(
    ({ cursor, runId }) => {
      const scope = window as any;
      scope.measurements = [];
      const events = new EventSource(`/v1/events?after=${cursor}`);
      scope.measureEvents = events;
      events.addEventListener("entryplug", (e) => {
        const event = JSON.parse((e as MessageEvent).data);
        if (
          event.run_id !== runId ||
          !["run.pause", "run.resume"].includes(event.type)
        )
          return;
        const expected = event.type === "run.pause" ? "PAUSED" : "ACTIVE";
        const detect = () => {
          if (
            document
              .querySelector(".run-status .status")
              ?.textContent?.includes(expected)
          ) {
            scope.measurements.push(Date.now() - Date.parse(event.at));
            return true;
          }
          return false;
        };
        if (!detect()) {
          const observer = new MutationObserver(() => {
            if (detect()) observer.disconnect();
          });
          observer.observe(document.querySelector("main")!, {
            subtree: true,
            childList: true,
            characterData: true,
          });
        }
      });
    },
    { cursor: state.cursor, runId: run.id },
  );
  for (let i = 0; i < 30; i++) {
    await request.post(`/v1/runs/${run.id}/${i % 2 ? "resume" : "pause"}`, {
      headers: headers(),
      data: {},
    });
    await page.waitForFunction(
      (count) => (window as any).measurements.length >= count,
      i + 1,
    );
  }
  const latencies: number[] = await page.evaluate(() => {
    (window as any).measureEvents.close();
    return (window as any).measurements;
  });
  await request.post(`/v1/runs/${run.id}/stop`, {
    headers: headers(),
    data: {},
  });
  latencies.sort((a, b) => a - b);
  const reconnects: number[] = [];
  for (let i = 0; i < 5; i++) {
    const start = Date.now();
    await page.reload();
    await expect(
      page.getByText("Service connected", { exact: true }),
    ).toBeVisible();
    reconnects.push(Date.now() - start);
  }
  reconnects.sort((a, b) => a - b);
  const graphPage = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  await graphPage.setContent(
    '<div id="graph" style="position:fixed;inset:0;background:#0b0e12"></div>',
  );
  await graphPage.addScriptTag({
    path: resolve("node_modules/cytoscape/dist/cytoscape.min.js"),
  });
  const graph = await graphPage.evaluate(async () => {
    const cy = (window as any).cytoscape({
      container: document.querySelector("#graph"),
      textureOnViewport: true,
      pixelRatio: 1,
      layout: { name: "preset" },
      elements: [
        ...Array.from({ length: 1000 }, (_, i) => ({
          data: { id: `n${i}`, label: `Capability ${i}` },
          position: { x: (i % 32) * 40, y: Math.floor(i / 32) * 30 },
        })),
        ...Array.from({ length: 2000 }, (_, i) => ({
          data: {
            id: `e${i}`,
            source: `n${i % 1000}`,
            target: `n${(i + (i < 1000 ? 1 : 32)) % 1000}`,
          },
        })),
      ],
      style: [
        {
          selector: "node",
          style: {
            label: "data(label)",
            shape: "ellipse",
            width: 12,
            height: 12,
            "font-size": 6,
            color: "#e3e0d4",
            "background-color": "#88dfcc",
          },
        },
        { selector: "edge", style: { width: 1, "line-color": "#647671" } },
      ],
    });
    cy.fit(undefined, 40);
    let batches = 0,
      frames = 0;
    const updates = setInterval(() => {
      cy.batch(() => {
        for (let i = 0; i < 10; i++)
          cy.getElementById(`n${(batches * 10 + i) % 1000}`).style(
            "background-color",
            batches % 2 ? "#88dfcc" : "#e4b46a",
          );
      });
      batches++;
    }, 200);
    const canvas = document.querySelector("#graph canvas")!;
    canvas.dispatchEvent(
      new MouseEvent("mousedown", {
        bubbles: true,
        clientX: 10,
        clientY: 10,
        button: 0,
        buttons: 1,
      }),
    );
    const start = performance.now();
    await new Promise<void>((finish) => {
      function frame(now: number) {
        frames++;
        window.dispatchEvent(
          new MouseEvent("mousemove", {
            bubbles: true,
            clientX: 40 + 25 * Math.sin(now / 1000),
            clientY: 20,
            buttons: 1,
          }),
        );
        if (now - start < 10000) requestAnimationFrame(frame);
        else finish();
      }
      requestAnimationFrame(frame);
    });
    window.dispatchEvent(
      new MouseEvent("mouseup", {
        bubbles: true,
        clientX: 40,
        clientY: 20,
        button: 0,
      }),
    );
    clearInterval(updates);
    const seconds = (performance.now() - start) / 1000;
    const output = {
      nodes: cy.nodes().length,
      edges: cy.edges().length,
      seconds,
      frames,
      fps: frames / seconds,
      update_batches: batches,
      nodes_per_batch: 10,
      relayouts: 0,
    };
    cy.destroy();
    return output;
  });
  expect(graph.nodes).toBe(1000);
  expect(graph.edges).toBe(2000);
  writeFileSync(
    "/tmp/entryplug-browser-metrics.json",
    JSON.stringify(
      {
        browser: browser.version(),
        viewport: "1440x1000",
        loopback_samples_ms: latencies,
        loopback_p95_ms: latencies[Math.ceil(latencies.length * 0.95) - 1],
        reconnect_samples_ms: reconnects,
        reconnect_p95_ms: reconnects[Math.ceil(reconnects.length * 0.95) - 1],
        graph,
      },
      null,
      2,
    ),
  );
  await graphPage.close();
});
