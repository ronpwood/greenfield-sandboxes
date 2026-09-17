// sandbox_mount/guest/app_proxy.ts — public :PORT -> the app's dev server on loopback.
//
// bun's HTML dev server carries a DNS-rebinding guard (DevServer.rs,
// is_allowed_host_header) that 403s any Host that is not localhost, *.localhost,
// an IP literal, or its configured hostname. The exe.dev proxy arrives as
// <vm>.exe.xyz. Rewriting Host to localhost satisfies the guard by the one rule
// it exists to enforce, so this survives dev-server policy changes that a flag
// would not. The dev server keeps its default loopback bind and its live
// rebundling; this file is the only wildcard bind on the box.
//
// App-agnostic: UPSTREAM is wherever the current app listens. A swap to an app
// with its own server changes the port, not this file.
//
// Known limitation: WebSocket upgrades (the dev server's HMR socket) are not
// proxied. Rebundling still happens on the next plain request, which is what
// the observe URL needs; a browser reload shows the new build.
//
// Usage (from observe.just): PORT=4501 UPSTREAM=4502 bun app_proxy.ts
const PORT = Number(process.env.PORT ?? 4501);
const UPSTREAM = Number(process.env.UPSTREAM ?? 4502);

const server = Bun.serve({
  port: PORT,
  hostname: "0.0.0.0",
  async fetch(req) {
    const url = new URL(req.url);
    url.protocol = "http:";
    url.hostname = "localhost";   // NOT 127.0.0.1 — the dev server binds `localhost`, which may be [::1]
    url.port = String(UPSTREAM);
    const headers = new Headers(req.headers);
    headers.set("host", `localhost:${UPSTREAM}`);
    headers.delete("origin");     // the guard checks Origin too when present
    return fetch(url, { method: req.method, headers, body: req.body, redirect: "manual" });
  },
});
console.log(`app_proxy: 0.0.0.0:${server.port} -> localhost:${UPSTREAM} (Host rewritten)`);
