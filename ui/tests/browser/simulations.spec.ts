import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

test.beforeEach(async ({ page, request }) => {
  const token = readFileSync(
    "/tmp/entryplug-playwright/config/client.token",
    "utf8",
  ).trim();
  const login = await (
    await request.post("/v1/session/bootstrap", {
      headers: {
        Authorization: `Bearer ${token}`,
        "Idempotency-Key": crypto.randomUUID(),
      },
    })
  ).json();
  await page.goto(`/#bootstrap=${login.bootstrap}`);
  await expect(
    page.getByText("Service connected", { exact: true }),
  ).toBeVisible();
});

test("camera recording plays and the simulated agent records tool evidence", async ({
  page,
}) => {
  await page.getByRole("button", { name: /Explore simulations/ }).click();
  await expect(
    page.getByRole("heading", { name: "Simulations", exact: true }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.locator("video").evaluate((v: HTMLVideoElement) => v.videoWidth),
    )
    .toBeGreaterThan(0);
  await page.getByRole("button", { name: /Run with simulated agent/ }).click();
  await expect(
    page.getByText("SIMULATED AGENT · NO LLM", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Simulated agent: labelled entry confirmed; camera evidence attached.",
      { exact: true },
    ),
  ).toBeVisible({ timeout: 10000 });
  await expect
    .poll(() =>
      page.locator("video").evaluate((v: HTMLVideoElement) => v.currentTime),
    )
    .toBeGreaterThan(2);
  await page.getByRole("button", { name: /Pause scenario/ }).click();
  await expect
    .poll(() =>
      page.locator("video").evaluate((v: HTMLVideoElement) => v.paused),
    )
    .toBe(true);
  await page.screenshot({
    path: "/tmp/entryplug-camera-workbench.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Stop mission", exact: true }).click();
});

test("a new rover can be added and the simulated agent moves it", async ({
  page,
}) => {
  await page.getByRole("button", { name: /Body & topology/ }).click();
  await page
    .getByRole("button", { name: /Add body/ })
    .first()
    .click();
  await page.getByRole("button", { name: "Rover", exact: true }).click();
  const name = `Browser rover ${Date.now()}`;
  await page.getByLabel("Body name", { exact: true }).fill(name);
  await page
    .getByRole("button", { name: "Add simulated body", exact: true })
    .click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await page.getByRole("button", { name: /Run with simulated agent/ }).click();
  await expect(
    page.getByText("Simulated rover reached aisle.", { exact: true }),
  ).toBeVisible({ timeout: 10000 });
  await page.getByRole("button", { name: /Pause scenario/ }).click();
  await page.getByRole("button", { name: "Add obstacle", exact: true }).click();
  await expect(
    page.getByText(
      "Simulated rover: obstacle detected; staying at the current waypoint.",
      { exact: true },
    ),
  ).toBeVisible({ timeout: 10000 });
  await page.screenshot({
    path: "/tmp/entryplug-rover-workbench.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(390);
  await page.screenshot({
    path: "/tmp/entryplug-simulation-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Stop mission", exact: true }).click();
});

test("agent setup stays accessible and missing login libraries explain the remedy", async ({
  page,
}) => {
  await page.route("**/v1/providers", (route) =>
    route.fulfill({
      json: {
        openai_installed: false,
        install_command:
          "python -m pip install './dist/entryplug-0.0.1-py3-none-any.whl[ui,openai]'",
        restart_required: true,
      },
    }),
  );
  await page
    .getByRole("button", { name: /New mission/ })
    .first()
    .click();
  const option = page.getByRole("button", { name: /Entryplug agent/ });
  await expect(option).toBeEnabled();
  await option.click();
  await expect(
    page.getByText("Install OpenAI support once", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Continue with ChatGPT", exact: true }),
  ).toBeDisabled();
  await page.screenshot({
    path: "/tmp/entryplug-agent-setup.png",
    fullPage: true,
  });
  await page.unroute("**/v1/providers");
  await page.getByRole("button", { name: /Local rule/ }).click();
  await option.click();
  await expect(
    page.getByRole("button", { name: "Continue with ChatGPT", exact: true }),
  ).toBeEnabled();
  await page.route("**/v1/auth/chatgpt/start", (route) =>
    route.fulfill({
      json: { authorization_url: "/#connections", profile: "personal-chatgpt" },
    }),
  );
  const request = page.waitForRequest("**/v1/auth/chatgpt/start");
  await page
    .getByRole("button", { name: "Continue with ChatGPT", exact: true })
    .click();
  expect((await request).postDataJSON().profile).toBe("personal-chatgpt");
});

test("room occupancy drives its light and protocol choices explain their bridge", async ({
  page,
}) => {
  await page.getByRole("button", { name: /Explore simulations/ }).click();
  await page
    .locator(".simulation-cards button")
    .filter({ hasText: "Smart room" })
    .click();
  await page.getByRole("button", { name: /Run with simulated agent/ }).click();
  await page.getByRole("button", { name: /Pause scenario/ }).click();
  await page
    .getByRole("button", { name: "Someone arrives", exact: true })
    .click();
  await expect(
    page.getByRole("img", {
      name: "Simulated room: occupied, light on",
      exact: true,
    }),
  ).toBeVisible();
  await page.screenshot({
    path: "/tmp/entryplug-room-workbench.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Stop mission", exact: true }).click();
  await page.getByRole("button", { name: /Body & topology/ }).click();
  await page
    .getByRole("button", { name: /Add body/ })
    .first()
    .click();
  for (const protocol of ["ROS 2", "Zenoh", "MQTT"]) {
    await page
      .getByRole("button", {
        name: `${protocol} Through an MCP bridge`,
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("heading", { name: `${protocol} via MCP`, exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("An MCP bridge is required", { exact: true }),
    ).toBeVisible();
  }
  await page.screenshot({
    path: "/tmp/entryplug-add-body.png",
    fullPage: true,
  });
});

test("failed body connection can be retried and lists the missions that use it", async ({
  page,
  request,
}) => {
  const token = readFileSync(
    "/tmp/entryplug-playwright/config/client.token",
    "utf8",
  ).trim();
  const headers = () => ({
    Authorization: `Bearer ${token}`,
    "Idempotency-Key": crypto.randomUUID(),
  });
  const name = `Offline light ${Date.now()}`;
  const response = await request.post("/v1/connections", {
    headers: headers(),
    data: {
      kind: "homeassistant",
      name,
      url: "ws://127.0.0.1:9998/api/websocket",
      entity_id: "light.entry",
      token_env: "ENTRYPLUG_ABSENT_BROWSER_TOKEN",
    },
  });
  expect(response.status()).toBe(202);
  const body = await response.json();
  await expect
    .poll(
      async () =>
        (
          await (
            await request.get("/v1/connections", { headers: headers() })
          ).json()
        ).find((c: { id: string }) => c.id === body.id)?.status,
    )
    .toBe("unverified");
  const missionName = `Dependent watch ${Date.now()}`;
  const mission = await request.post("/v1/missions", {
    headers: headers(),
    data: {
      name: missionName,
      instructions: "Wait for a verified light",
      body: { selector: body.id, required_capabilities: [] },
      trigger: { kind: "manual", source: body.id },
      access: { allow: [] },
    },
  });
  expect(mission.status()).toBe(202);
  await page.getByRole("button", { name: /Connections/ }).click();
  const card = page.locator(".connection-entry").filter({ hasText: name });
  await expect(
    card.getByRole("button", { name: "Retry connection" }),
  ).toBeVisible();
  await card.getByText("Used by 1 missions").click();
  await expect(card.getByText(missionName, { exact: true })).toBeVisible();
  const retry = page.waitForRequest(`**/v1/connections/${body.id}/retry`);
  await card.getByRole("button", { name: "Retry connection" }).click();
  await retry;
  await expect
    .poll(
      async () =>
        (
          await (
            await request.get("/v1/connections", { headers: headers() })
          ).json()
        ).find((c: { id: string }) => c.id === body.id)?.status,
    )
    .toBe("unverified");
  await page.screenshot({
    path: "/tmp/entryplug-connection-diagnostics.png",
    fullPage: true,
  });
});
