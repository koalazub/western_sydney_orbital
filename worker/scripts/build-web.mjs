import { build } from "esbuild";
import { mkdir, copyFile, readFile, writeFile, rm } from "node:fs/promises";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const source = resolve(root, "web");
const output = resolve(root, "public");

await mkdir(output, { recursive: true });
// Remove the retired generated runner from incremental builds, not other assets.
await rm(resolve(output, "python-worker.js"), { force: true });
await Promise.all([
  copyFile(resolve(source, "index.html"), resolve(output, "index.html")),
  copyFile(resolve(source, "notebook.html"), resolve(output, "notebook.html")),
  copyFile(resolve(source, "styles.css"), resolve(output, "styles.css")),
  copyFile(resolve(source, ".assetsignore"), resolve(output, ".assetsignore")),
]);
// Prepared mission imagery is checked in. Builds validate and copy local files;
// neither this script nor the asset-preparation script downloads anything.
const assetSource = resolve(source, "assets");
const assetOutput = resolve(output, "assets");
const manifest = JSON.parse(await readFile(resolve(assetSource, "asset-manifest.json"), "utf8"));
const expectedMaps = new Set([
  "textures/phobos-viking-albedo.png",
  "textures/mars-viking-albedo.png",
  "textures/stars-nasa-svs-cube.png",
]);
if (manifest.schema !== 1 || manifest.textures.length !== expectedMaps.size) {
  throw new Error("Planetary asset manifest does not contain the required texture maps");
}
for (const asset of manifest.textures) {
  if (!expectedMaps.delete(asset.path)) throw new Error(`Unexpected planetary asset ${asset.path}`);
  const bytes = await readFile(resolve(assetSource, asset.path));
  if (bytes.length !== asset.bytes || bytes.length >= 25 * 1024 * 1024
      || createHash("sha256").update(bytes).digest("hex") !== asset.sha256
      || bytes.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a"
      || bytes.readUInt32BE(16) !== asset.width || bytes.readUInt32BE(20) !== asset.height) {
    throw new Error(`Planetary asset ${asset.path} differs from its prepared manifest`);
  }
  const target = resolve(assetOutput, asset.path);
  await mkdir(dirname(target), { recursive: true });
  try {
    if ((await readFile(target)).equals(bytes)) continue;
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  // Publish the bytes validated above, not a second, possibly changed source read.
  await writeFile(target, bytes);
}
await Promise.all([
  copyFile(resolve(assetSource, "asset-manifest.json"), resolve(assetOutput, "asset-manifest.json")),
  copyFile(resolve(assetSource, "ATTRIBUTION.md"), resolve(assetOutput, "ATTRIBUTION.md")),
  // build:licenses owns dependency notices; imagery credits stay separate.
  copyFile(resolve(assetSource, "ATTRIBUTION.md"), resolve(output, "asset-notices.txt")),
]);
await build({
  entryPoints: [resolve(source, "app.ts")],
  outfile: resolve(output, "app.js"),
  bundle: true,
  external: ["/sim/*"],
  format: "esm",
  target: "es2022",
  minify: true,
});
console.log("Built worker/public browser app (preserved existing public assets).");
