import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { get } from "node:http";
import { dirname, relative, resolve, sep } from "node:path";

const DEFAULT_TOKEN_FILE = ".devenv/wsorb-marimo.token";
const MAX_TOKEN_BYTES = 44;
const PUBLIC_DIRECTORIES = ["worker/public", "worker/web", "dist"];
const PRIVATE_FILE = "Marimo token must be an owned private regular file, mode 0600";

const inside = (root, path) => {
  const rel = relative(root, path);
  return !rel || (rel !== ".." && !rel.startsWith(`..${sep}`) && !rel.startsWith(sep));
};

function labPort() {
  const value = process.env.WSORB_LAB_PORT ?? "2718";
  if (!/^[1-9][0-9]{0,4}$/.test(value) || Number(value) > 65535) {
    throw new Error("WSORB_LAB_PORT must be an integer from 1 to 65535");
  }
  return Number(value);
}

function checkFile(info) {
  if (!info.isFile() || (info.mode & 0o7777) !== 0o600 || info.uid !== process.getuid()
    || info.nlink !== 1 || ![43, MAX_TOKEN_BYTES].includes(info.size)) {
    throw new Error(PRIVATE_FILE);
  }
}

async function readToken(root) {
  if (typeof constants.O_NOFOLLOW !== "number" || typeof process.getuid !== "function") {
    throw new Error("The local lab requires POSIX private-file protections");
  }
  const configured = process.env.WSORB_LAB_TOKEN_FILE ?? DEFAULT_TOKEN_FILE;
  if (!configured) throw new Error("WSORB_LAB_TOKEN_FILE must name a private token file");
  const requested = resolve(root, configured);
  for (const directory of PUBLIC_DIRECTORIES) {
    const publicPath = resolve(root, directory);
    const actual = await realpath(publicPath).catch(error => {
      if (error.code === "ENOENT") return publicPath;
      throw error;
    });
    if (inside(publicPath, requested) || inside(actual, requested)) {
      throw new Error("Marimo token file must be outside public assets");
    }
  }
  for (let parent = dirname(requested);; parent = dirname(parent)) {
    const info = await lstat(parent);
    if (!info.isDirectory()) throw new Error("Marimo token directories must not use symlinks");
    if (parent === dirname(requested) && (info.mode & 0o022)) {
      throw new Error("Marimo token directory must not be group or world writable");
    }
    if (parent === dirname(parent)) break;
  }
  const info = await lstat(requested);
  checkFile(info);
  const file = await open(requested, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const opened = await file.stat();
    checkFile(opened);
    if (opened.dev !== info.dev || opened.ino !== info.ino) {
      throw new Error("Marimo token file changed while opening it");
    }
    const buffer = Buffer.alloc(MAX_TOKEN_BYTES + 1);
    const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
    checkFile(await file.stat());
    const content = buffer.subarray(0, bytesRead);
    const token = content.subarray(0, 43).toString("latin1");
    if (!/^[A-Za-z0-9_-]{43}$/.test(token)
      || (bytesRead !== 43 && (bytesRead !== 44 || content[43] !== 10))) {
      throw new Error("Marimo token file must contain one 43-character ASCII token");
    }
    return token;
  } finally {
    await file.close();
  }
}

function ready(port) {
  return new Promise(resolveReady => {
    let timer;
    const finish = result => {
      clearTimeout(timer);
      resolveReady(result);
    };
    const request = get({ hostname: "127.0.0.1", port, path: "/", agent: false }, response => {
      // Marimo 0.25.2 sends an unauthenticated root GET to this relative login URL.
      const login = response.statusCode === 303 && response.headers.location === "/auth/login?next=%2F";
      response.destroy();
      finish(response.statusCode === 200 || login);
    });
    request.on("error", () => finish(false));
    timer = setTimeout(() => {
      request.destroy();
      finish(false);
    }, 750);
  });
}

/**
 * Server-only handoff: await resolveNotebook({ root: repositoryRoot }).
 * Uses WSORB_LAB_TOKEN_FILE relative to root, default .devenv/wsorb-marimo.token,
 * and WSORB_LAB_PORT, default 2718. Missing files or an unready editor return null.
 * Unsafe files/configuration throw fixed messages without secrets or file paths.
 * The caller owns route authentication and redirect headers. Never publish this
 * result as JSON or static assets. This function does not start any process.
 */
export async function resolveNotebook({ root }) {
  const port = labPort();
  let token;
  try {
    token = await readToken(await realpath(root));
  } catch (error) {
    if (error.code === "ENOENT") return null;
    if (error.code) throw new Error("Cannot read the private Marimo token file");
    throw error;
  }
  if (!await ready(port)) return null;
  return { url: `http://localhost:${port}/?access_token=${token}` };
}
