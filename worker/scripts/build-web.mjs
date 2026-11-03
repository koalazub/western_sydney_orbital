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
  copyFile(resolve(source, "compare.html"), resolve(output, "compare.html")),
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
    let displayBytes = bytes;
    if (name === "score-components.svg") {
      // Keep the historical publication intact. Its longest negative tick is
      // outside the old viewport; pad only this served presentation copy.
      const svg = bytes.toString("utf8");
      if (!/viewBox="0 0 1100 \d+"/.test(svg)) throw new Error("Unknown archived score chart geometry");
      displayBytes = Buffer.from(svg.replace(/viewBox="0 0 1100 (\d+)"/,
        'style="background:#fff" viewBox="-32 0 1164 $1"'));
    }
    await writeFile(resolve(analyticsOutput, targetName), displayBytes);
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

// Curated trajectories are a separate publication. Never ship a raw sweep or
// infer a new cohort from the older summary. Check byte identity and the same
// strict parser the browser uses before publishing either fixed public URL.
const pathsSource = resolve(root, "..", "docs", "analytics", "path-comparison");
const pathsManifest = JSON.parse(await readFile(resolve(pathsSource, "manifest.json"), "utf8"));
const pathParserBundle = await build({
  entryPoints: [resolve(source, "path-comparison-data.ts")], bundle: true, write: false,
  format: "esm", platform: "neutral", target: "es2022", minify: true,
});
const pathParser = await import(`data:text/javascript;base64,${Buffer.from(pathParserBundle.outputFiles[0].contents).toString("base64")}`);
const pathPublications = new Map();
for (const id of ["official", "team"]) {
  const name = `${id}.json`, path = resolve(pathsSource, name);
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 4 * 1024 * 1024) throw new Error("Invalid path publication asset");
  const bytes = await readFile(path);
  const entry = pathsManifest.artifacts.find(item => item.path === name);
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  if (!entry || entry.bytes !== bytes.length || entry.sha256 !== sha256) throw new Error(`Path publication hash mismatch: ${id}`);
  const data = pathParser.validatePathComparison(JSON.parse(bytes.toString("utf8")));
  if (!data || data.id !== id) throw new Error(`Unsupported path comparison: ${id}`);
  pathPublications.set(id, { data, sha256 });
  await writeFile(resolve(output, "analytics", `paths-${id}.json`), bytes);
}
const correctedTrialsPath = resolve(pathsSource, "official-trials.csv");
const correctedTrialsInfo = await lstat(correctedTrialsPath);
if (!correctedTrialsInfo.isFile() || correctedTrialsInfo.isSymbolicLink() || correctedTrialsInfo.size > 128 * 1024) throw new Error("Invalid corrected trial table");
const correctedTrials = await readFile(correctedTrialsPath);
const correctedEntry = pathsManifest.artifacts.find(item => item.path === "official-trials.csv");
if (!correctedEntry || correctedEntry.bytes !== correctedTrials.length
  || correctedEntry.sha256 !== createHash("sha256").update(correctedTrials).digest("hex")) throw new Error("Corrected trial table hash mismatch");
await writeFile(resolve(output, "analytics", "paths-official-trials.csv"), correctedTrials);
await copyFile(resolve(root, "..", "docs", "python-flight-theory.md"), resolve(output, "analytics", "python-flight-theory.txt"));
await copyFile(resolve(pathsSource, "manifest.json"), resolve(output, "analytics", "paths-manifest.json"));

// Optional metrics have their own manifest. Validate the exact browser DTO and
// its join to the unchanged path publication before copying any supplemental bytes.
const metricsSource = resolve(root, "..", "docs", "analytics", "path-metrics");
const metricIds = ["official", "team"];
let metricsAvailable = false;
try {
  const info = await lstat(metricsSource);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("Metrics publication must be a real directory");
  metricsAvailable = true;
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
const metricFiles = [];
let metricsManifestBytes = null;
if (metricsAvailable) {
  const metricParserBundle = await build({
    entryPoints: [resolve(source, "comparison-model.ts")], bundle: true, write: false,
    format: "esm", platform: "neutral", target: "es2022", minify: true,
  });
  const metricParser = await import(`data:text/javascript;base64,${Buffer.from(metricParserBundle.outputFiles[0].contents).toString("base64")}`);
  const manifestPath = resolve(metricsSource, "manifest.json");
  const manifestInfo = await lstat(manifestPath);
  if (!manifestInfo.isFile() || manifestInfo.isSymbolicLink() || manifestInfo.size > 65536) throw new Error("Invalid metrics manifest asset");
  metricsManifestBytes = await readFile(manifestPath);
  if (metricsManifestBytes.length > 65536) throw new Error("Metrics manifest exceeds its byte budget");
  const metricsManifest = JSON.parse(metricsManifestBytes.toString("utf8"));
  const shape = (value, keys) => value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  if (!shape(metricsManifest, ["schema", "schema_version", "artifacts"])
    || metricsManifest.schema !== "wsorb.comparison_metrics.artifacts" || metricsManifest.schema_version !== 1
    || !Array.isArray(metricsManifest.artifacts) || metricsManifest.artifacts.length > metricIds.length) throw new Error("Unsupported metrics manifest");
  const seen = new Set();
  for (const entry of metricsManifest.artifacts) {
    if (!shape(entry, ["id", "path", "bytes", "sha256"]) || !metricIds.includes(entry.id) || seen.has(entry.id)
      || entry.path !== `${entry.id}.json` || !Number.isSafeInteger(entry.bytes) || entry.bytes < 1 || entry.bytes > metricParser.MAX_METRIC_BYTES
      || typeof entry.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(entry.sha256)) throw new Error("Invalid metrics manifest artifact");
    seen.add(entry.id);
    const path = resolve(metricsSource, entry.path), info = await lstat(path);
    if (!info.isFile() || info.isSymbolicLink() || info.size > metricParser.MAX_METRIC_BYTES) throw new Error("Invalid metrics publication asset");
    const bytes = await readFile(path);
    if (bytes.length !== entry.bytes || createHash("sha256").update(bytes).digest("hex") !== entry.sha256) throw new Error(`Metrics publication hash mismatch: ${entry.id}`);
    const paths = pathPublications.get(entry.id);
    const data = metricParser.validateMetricPublication(JSON.parse(bytes.toString("utf8")), paths.data, paths.sha256);
    if (!data || data.source.path_sha256 !== paths.sha256) throw new Error(`Metrics publication source mismatch: ${entry.id}`);
    metricFiles.push({ id: entry.id, bytes });
  }
}
for (const id of metricIds) {
  const file = metricFiles.find(file => file.id === id), target = resolve(output, "analytics", `metrics-${id}.json`);
  if (file) await writeFile(target, file.bytes);
  else await rm(target, { force: true });
}
const metricsManifestTarget = resolve(output, "analytics", "metrics-manifest.json");
if (metricsManifestBytes) await writeFile(metricsManifestTarget, metricsManifestBytes);
else await rm(metricsManifestTarget, { force: true });

await build({
  entryPoints: [resolve(source, "comparison.ts")], outfile: resolve(output, "comparison.js"),
  bundle: true, format: "esm", target: "es2022", minify: true,
});

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
