import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "jsonc-parser";

const worker = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const errors = [];
const config = parse(readFileSync(resolve(worker, "wrangler.jsonc"), "utf8"), errors);
if (errors.length || !/^[a-f0-9]{32}$/.test(config?.account_id ?? "")) {
  console.error("Set a valid project account in worker/wrangler.jsonc.");
  process.exit(1);
}
const current = process.versions.node.split(".").map(Number);
if (current[0] !== 26 || current[1] < 12 || (current[1] === 12 && current[2] < 1)) {
  console.error("Use the project environment, which provides security-patched Node.js 26.12.1.");
  process.exit(1);
}

const env = { ...process.env, CLOUDFLARE_ACCOUNT_ID: config.account_id };
const binary = name => resolve(worker, "node_modules", ".bin", name);
const [action, ...args] = process.argv.slice(2);

if (action === "account") {
  const cf = spawnSync(binary("cf"), ["auth", "whoami"], { env, encoding: "utf8" });
  let identity;
  try { identity = JSON.parse(cf.stdout || "{}"); } catch { identity = {}; }
  if (cf.status !== 0 || !identity.authenticated || !identity.accounts?.some(account => account.id === config.account_id)) {
    console.error("cf is not authorized for this project's account. Run make login-cf with the same account as Wrangler.");
    process.exit(1);
  }
  const wrangler = spawnSync(binary("wrangler"), ["whoami"], { env, encoding: "utf8" });
  if (wrangler.status !== 0 || !wrangler.stdout?.includes(config.account_id)) {
    console.error("Wrangler is not authorized for this project's account. Run make login-wrangler.");
    process.exit(1);
  }
  console.log(`cf and Wrangler are authorized for project account ${config.account_id}.`);
} else if (action === "cf" || action === "wrangler") {
  const result = spawnSync(binary(action), args, { env, stdio: "inherit" });
  if (result.error) console.error(result.error.message);
  process.exit(result.status ?? 1);
} else {
  console.error("Choose cf, wrangler, or account.");
  process.exit(1);
}
