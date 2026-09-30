import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

test("embark, observe, close browser, control by CLI, and retain alerts", async ({
  page,
  request,
  browser,
}) => {
  const token = readFileSync(
    "/tmp/entryplug-playwright/config/client.token",
    "utf8",
  ).trim();
  const headers = {
    Authorization: `Bearer ${token}`,
    "Idempotency-Key": crypto.randomUUID(),
  };
  const bootstrap = await request.post("/v1/session/bootstrap", { headers });
  const { bootstrap: secret } = await bootstrap.json();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`/#bootstrap=${secret}`);
  await expect(
    page.getByText("Service connected", { exact: true }),
  ).toBeVisible();
  expect(page.url()).not.toContain("bootstrap");
  await page
    .getByRole("button", { name: "◇ Embark", exact: false })
    .first()
    .click();
  await page.getByRole("button", { name: "Continue →", exact: true }).click();
  await page.getByRole("button", { name: "Continue →", exact: true }).click();
  const name = `Browser watch ${Date.now()}`;
  await page.getByLabel("Mission name", { exact: true }).fill(name);
  await page.getByRole("button", { name: "Validate & review" }).click();
  await page.getByRole("button", { name: "Launch mission" }).click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await page.getByText("Simulated source controls", { exact: true }).click();
  await page
    .getByRole("button", { name: "Person A enters", exact: true })
    .click();
  await expect(
    page.getByText("Person entered the entrance zone", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() => scrollTo(0, 0));
  // A lazily mounted graph must paint before the screenshot or pointer check.
  await expect
    .poll(() =>
      page.locator(".graph").evaluate((element) =>
        [...element.querySelectorAll("canvas")].some((canvas) => {
          if (canvas.width < 2 || canvas.height < 2) return false;
          const pixels = canvas
            .getContext("2d")!
            .getImageData(0, 0, canvas.width, canvas.height).data;
          return pixels.some((value, index) => index % 4 === 3 && value > 0);
        }),
      ),
    )
    .toBe(true);
  await page.locator(".graph").click({ position: { x: 100, y: 80 } });
  await expect(
    page.getByText("Access camera · SIMULATED", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({
    path: "/tmp/entryplug-mission-desktop.png",
    fullPage: true,
  });
  const state = await (await request.get("/v1/snapshot", { headers })).json();
  const run = state.runs.find((item: any) => item.name === name);
  await page.close();
  const output = execFileSync(
    process.env.ENTRYPLUG_TEST_PYTHON || "python3",
    [
      "-m",
      "entryplug",
      "mission",
      "pause",
      run.id,
      "--workspace",
      "/tmp/entryplug-playwright",
      "--json",
    ],
    { cwd: "..", env: { ...process.env, PYTHONPATH: "src" }, encoding: "utf8" },
  );
  expect(JSON.parse(output).lifecycle).toBe("paused");
  const next = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const reconnect = await (
    await request.post("/v1/session/bootstrap", { headers })
  ).json();
  await next.goto(`/#bootstrap=${reconnect.bootstrap}`);
  await expect(
    next.getByText("Service connected", { exact: true }),
  ).toBeVisible();
  await next.getByRole("button", { name: /Inbox/ }).click();
  await expect(
    next.getByText("Person entered the entrance zone", { exact: true }).first(),
  ).toBeVisible();
  await next.screenshot({
    path: "/tmp/entryplug-inbox-mobile.png",
    fullPage: true,
  });
  expect(
    await next.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await next
    .getByRole("button", { name: "Acknowledge", exact: true })
    .first()
    .focus();
  await next.keyboard.press("Enter");
  await expect(
    next.getByRole("button", { name: "Acknowledge", exact: true }).first(),
  ).toBeDisabled();
  await request.post(`/v1/runs/${run.id}/stop`, {
    headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
    data: {},
  });
  expect(
    (await (await request.get(`/v1/runs/${run.id}`, { headers })).json())
      .lifecycle,
  ).toBe("stopped");
  expect(errors).toEqual([]);
  await next.close();
});
