import { tmpdir } from "node:os";

// Use one runtime selection for the browser server and its CLI parity checks.
const installed = process.env.ENTRYPLUG_TEST_INSTALLED === "1";

export const testRuntime = {
  python: process.env.ENTRYPLUG_TEST_PYTHON || "python3",
  pythonArgs: installed ? ["-I"] : [],
  cwd: installed ? tmpdir() : "..",
  env: { PYTHONPATH: installed ? "" : "src" },
};
