import { build } from "esbuild";
import { mkdir, copyFile, readFile, writeFile, rm, lstat } from "node:fs/promises";
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
// Only the curated, measurement-only publication is shipped. Never copy an
// experiment directory, traces, Python sources, descriptors or camera captures.
const analyticsSource = resolve(root, "..", "docs", "analytics", "team-comparison");
let publishedAnalytics = false;
try {
  const info = await lstat(analyticsSource);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("Analytics publication must be a real directory");
  publishedAnalytics = true;
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
if (publishedAnalytics) {
  const analyticsOutput = resolve(output, "analytics");
  await mkdir(analyticsOutput, { recursive: true });
  // Exercise the actual read-only browser data parser, without a DOM, engine or
  // renderer substitute. This catches schema drift before shipping a dataset.
  const parserBundle = await build({
    entryPoints: [resolve(source, "flight-analytics.ts")], bundle: true, write: false,
    format: "esm", platform: "neutral", target: "es2022", minify: true,
  });
  const parser = await import(`data:text/javascript;base64,${Buffer.from(parserBundle.outputFiles[0].contents).toString("base64")}`);
  for (const name of ["dataset.json", "trials.csv", "aggregate.csv", "score-components.svg", "fuel-burns.svg", "turns.svg", "efficiency.svg"]) {
    const path = resolve(analyticsSource, name);
    const info = await lstat(path);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 2 * 1024 * 1024) throw new Error("Invalid curated analytics asset");
    const bytes = await readFile(path);
    if (name === "dataset.json") {
      const data = JSON.parse(bytes.toString("utf8"));
      if (data.schema !== "wsorb.teammate_analytics" || ![1, 2].includes(data.schema_version)
        || !Array.isArray(data.trials) || !Array.isArray(data.profiles)
        || !parser.validatePublication(data)) throw new Error("Unsupported curated analytics dataset");
    }
    const targetName = name === "dataset.json" ? "team-comparison.json" : name === "aggregate.csv" ? "summary.csv" : name;
    await writeFile(resolve(analyticsOutput, targetName), bytes);
  }
}
// The official comparison dashboard is generated from the committed metrics.
// Publish the validated SVG beside the existing curated analytics assets.
const officialSource = resolve(root, "..", "docs", "analytics", "official-comparison", "performance.svg");
const officialTarget = resolve(output, "analytics", "official-comparison.svg");
const officialInfo = await lstat(officialSource);
if (!officialInfo.isFile() || officialInfo.isSymbolicLink() || officialInfo.size > 2 * 1024 * 1024) {
  throw new Error("Invalid official comparison dashboard");
}
const officialBytes = await readFile(officialSource);
if (officialBytes.subarray(0, 4).toString("utf8") !== "<svg") {
  throw new Error("Official comparison dashboard is not SVG");
}
await mkdir(dirname(officialTarget), { recursive: true });
await writeFile(officialTarget, officialBytes);

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
