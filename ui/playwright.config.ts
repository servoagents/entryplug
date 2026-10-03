import { defineConfig } from "@playwright/test";
import { testRuntime } from "./test-runtime";

export default defineConfig({
  testDir: "./tests/browser",
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL: "http://127.0.0.1:8877",
    headless: true,
    launchOptions: {
      executablePath:
        process.env.ENTRYPLUG_TEST_BROWSER === ""
          ? undefined
          : process.env.ENTRYPLUG_TEST_BROWSER || "/opt/google/chrome/chrome",
    },
    viewport: { width: 1440, height: 1000 },
    trace: "off",
  },
  webServer: {
    command: `${testRuntime.python} ${testRuntime.pythonArgs.join(" ")} -m entryplug serve --workspace /tmp/entryplug-playwright --port 8877`,
    cwd: testRuntime.cwd,
    env: testRuntime.env,
    port: 8877,
    reuseExistingServer: false,
  },
});
