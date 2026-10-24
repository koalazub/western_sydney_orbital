import { createReadStream } from "node:fs";
import { lstat, readFile, realpath } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const worker = resolve(fileURLToPath(new URL("..", import.meta.url)));
const publicRoot = resolve(worker, "public");
const port = Number(process.env.PORT ?? 8787);
if (!Number.isInteger(port) || port < 0 || port > 65535) {
  throw new Error("PORT must be an integer from 0 to 65535.");
}

try {
  if ((await lstat(publicRoot)).isSymbolicLink()) throw new Error("public root must not be a symlink");
  await lstat(resolve(publicRoot, "index.html"));
} catch {
  console.error("Built game assets are missing. Run `just build` before starting the local server.");
  process.exitCode = 1;
}

if (!process.exitCode) {
  const rootRealPath = await realpath(publicRoot);
  const lab = process.env.WSORB_LAB === "1"
    ? await (await import("./local-api.mjs")).createLocalApi({ worker, publicRoot, rootRealPath })
    : null;
  const contentTypes = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
  };
  const server = createServer(async (request, response) => {
    const headers = {
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
      "X-Frame-Options": "DENY",
    };
    const finish = (status, body = "") => {
      response.writeHead(status, headers);
      response.end(request.method === "HEAD" ? undefined : body);
    };

    if (lab) {
      try {
        if (!lab.acceptsHost(request)) {
          finish(403, "Local lab requires its exact loopback Host");
          return;
        }
        const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
        if (pathname === "/notebook.html") {
          await lab.handle(request, response, "/__local/notebook");
          return;
        }
        if (pathname === "/__local" || pathname.startsWith("/__local/")) {
          await lab.handle(request, response, pathname);
          return;
        }
      } catch {
        finish(404, "Not found");
        return;
      }
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      response.setHeader("Allow", "GET, HEAD");
      finish(405, "Method not allowed");
      return;
    }

    try {
      const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
      const segments = pathname === "/" ? [] : pathname.slice(1).split("/");
      if (pathname.includes("\\") || segments.some(part => !part || part === "." || part === ".." || part.startsWith("."))) {
        finish(404, "Not found");
        return;
      }
      const relativePath = pathname === "/" ? "index.html" : segments.join("/");
      const filePath = resolve(publicRoot, relativePath);
      const rel = relative(rootRealPath, filePath);
      if (!rel || rel === ".." || rel.startsWith(`..${sep}`) || resolve(filePath) !== filePath) {
        finish(404, "Not found");
        return;
      }

      let currentPath = publicRoot;
      for (const segment of relativePath.split(/[\\/]/)) {
        currentPath = resolve(currentPath, segment);
        const info = await lstat(currentPath);
        if (info.isSymbolicLink()) throw new Error("Symlinks are not served");
      }
      const actualPath = await realpath(filePath);
      if (!actualPath.startsWith(`${rootRealPath}${sep}`)) throw new Error("Path outside public root");
      const info = await lstat(actualPath);
      if (!info.isFile()) throw new Error("Not a file");
      if (lab && relativePath === "index.html") {
        const html = await readFile(actualPath, "utf8");
        const body = /<\/body>/i.test(html)
          ? html.replace(/<\/body>/i, `${lab.indexScript}\n</body>`)
          : `${html}\n${lab.indexScript}\n`;
        response.writeHead(200, {
          ...headers,
          "Content-Type": contentTypes[".html"],
          "Content-Length": Buffer.byteLength(body),
          "Cache-Control": "no-store",
          "Cross-Origin-Resource-Policy": "same-origin",
        });
        response.end(request.method === "HEAD" ? undefined : body);
        return;
      }
      const etag = `W/"${info.size.toString(16)}-${info.mtimeMs.toString(16)}"`;
      const cacheHeaders = {
        "Cache-Control": "public, max-age=0, must-revalidate",
        ETag: etag,
        "Last-Modified": info.mtime.toUTCString(),
      };
      const validators = request.headers["if-none-match"];
      if (typeof validators === "string" && validators.split(",").some(value => {
        const candidate = value.trim();
        return candidate === "*" || candidate.replace(/^W\//, "") === etag.slice(2);
      })) {
        response.writeHead(304, { ...headers, ...cacheHeaders });
        response.end();
        return;
      }
      response.writeHead(200, {
        ...headers,
        ...cacheHeaders,
        "Content-Type": contentTypes[extname(actualPath).toLowerCase()] ?? "application/octet-stream",
        "Content-Length": info.size,
      });
      if (request.method === "HEAD") response.end();
      else createReadStream(actualPath).pipe(response);
    } catch {
      finish(404, "Not found");
    }
  });

  if (lab) {
    server.maxConnections = 32;
    server.requestTimeout = 10000;
    server.headersTimeout = 5000;
    server.keepAliveTimeout = 1000;
    server.setTimeout(30000, socket => socket.destroy());
  }
  server.on("error", error => {
    console.error(`Local server failed: ${error.message}`);
    process.exitCode = 1;
    if (lab) void shutdown();
  });
  let stopping = false;
  const shutdown = lab ? async () => {
    if (stopping) return;
    stopping = true;
    try { await lab.close(); }
    catch { console.error("Could not remove the local connection descriptor safely."); }
    server.close(() => process.exit(process.exitCode ?? 0));
    server.closeAllConnections();
  } : () => server.close(() => process.exit());
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
  server.listen(port, "127.0.0.1", async () => {
    if (stopping) return;
    const address = server.address();
    if (lab) {
      try { await lab.listen(address.port); }
      catch (error) {
        console.error(`Local lab startup failed: ${error.message}`);
        process.exitCode = 1;
        await shutdown();
        return;
      }
      if (stopping) return;
    }
    console.log(`Western Sydney Orbital: http://127.0.0.1:${address.port}/`);
    console.log("Press Ctrl+C to stop.");
  });
}
