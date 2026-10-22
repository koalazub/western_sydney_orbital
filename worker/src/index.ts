export default {
  fetch(request) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return Response.json(
        { error: "Method not allowed" },
        { status: 405, headers: { Allow: "GET, HEAD" } },
      );
    }

    const { pathname } = new URL(request.url);
    if (pathname !== "/" && pathname !== "/health") {
      return Response.json({ error: "Not found" }, { status: 404 });
    }

    return Response.json(
      {
        service: "western-sydney-orbital",
        status: "ready",
        simulation_connected: false,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  },
} satisfies ExportedHandler<Env>;
