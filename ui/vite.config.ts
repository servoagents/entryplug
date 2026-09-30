import { defineConfig } from "vite";
import solid from "vite-plugin-solid";
import { readFileSync } from "node:fs";

export default defineConfig({
  plugins: [
    solid(),
    {
      name: "runtime-licenses",
      generateBundle() {
        const source = ["solid-js", "cytoscape"]
          .map(
            (name) =>
              `${name}\n${readFileSync(new URL(`./node_modules/${name}/LICENSE`, import.meta.url), "utf8")}`,
          )
          .join("\n\n");
        this.emitFile({
          type: "asset",
          fileName: "assets/third-party-licenses.txt",
          source,
        });
      },
    },
  ],
  build: {
    outDir: "../src/entryplug_ui/static",
    emptyOutDir: true,
    target: "es2022",
  },
});
