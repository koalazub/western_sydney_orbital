import { build } from "esbuild";
import { mkdir, copyFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const source = resolve(root, "web");
const output = resolve(root, "public");

await mkdir(output, { recursive: true });
await Promise.all([
  copyFile(resolve(source, "index.html"), resolve(output, "index.html")),
  copyFile(resolve(source, "styles.css"), resolve(output, "styles.css")),
  copyFile(resolve(source, ".assetsignore"), resolve(output, ".assetsignore")),
]);
await build({
  entryPoints: [resolve(source, "app.ts")],
  outfile: resolve(output, "app.js"),
  bundle: true,
  format: "esm",
  target: "es2022",
  minify: true,
});
await build({
  entryPoints: [resolve(source, "python-worker.ts")],
  outfile: resolve(output, "python-worker.js"),
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2022",
  minify: true,
});
console.log("Built worker/public browser app (preserved existing public assets).");
