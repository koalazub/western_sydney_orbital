import { execFileSync } from "node:child_process";
import { readFile, readdir, mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

const worker = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repository = resolve(worker, "..");
const sections = ["Western Sydney Orbital: third-party dependency notices\n"];

async function addNotice(name, version, license, directory, extraFile) {
  const files = new Set((await readdir(directory, { withFileTypes: true }))
    .filter(entry => entry.isFile() && /^(licen[cs]e|copying|notice)([._-]|$)/i.test(entry.name))
    .map(entry => join(directory, entry.name)));
  if (extraFile) files.add(resolve(directory, extraFile));
  sections.push(`\n${name} ${version}\nLicense: ${license || "See upstream distribution"}\n`);
  for (const file of [...files].sort()) {
    try { sections.push(await readFile(file, "utf8")); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
  }
}

const metadata = JSON.parse(execFileSync("cargo", [
  "metadata", "--locked", "--format-version", "1", "--filter-platform", "wasm32-unknown-unknown",
  "--manifest-path", join(repository, "simulation/Cargo.toml"),
], { cwd: repository, maxBuffer: 32 * 1024 * 1024, encoding: "utf8" }));
const included = new Set(metadata.resolve.nodes.map(node => node.id));
for (const pkg of metadata.packages) {
  if (included.has(pkg.id) && pkg.name !== "wsorb-bevy") {
    await addNotice(pkg.name, pkg.version, pkg.license, dirname(pkg.manifest_path), pkg.license_file);
  }
}

const lock = JSON.parse(await readFile(join(worker, "package-lock.json"), "utf8"));
for (const [path, pkg] of Object.entries(lock.packages)) {
  if (!path.startsWith("node_modules/") || pkg.dev === true) continue;
  const directory = join(worker, path);
  const info = JSON.parse(await readFile(join(directory, "package.json"), "utf8"));
  await addNotice(info.name, info.version, info.license, directory);
}
await mkdir(join(worker, "public"), { recursive: true });
await writeFile(join(worker, "public/licenses.txt"), sections.join("\n"));
console.log("Included dependency notices with the browser assets.");
