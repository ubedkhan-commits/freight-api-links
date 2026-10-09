# Playwright MCP — "Could not connect to your MCP server / fetch failed"

n8n `2.34.6 (Self Hosted)`, failing in sub-node `Playwright Browser (MCP)`:

```
Could not connect to your MCP server
fetch failed
{ "name": "TypeError", "message": "fetch failed" }
```

`fetch failed` is n8n collapsing every network-level failure into one message: DNS
failure, connection refused, wrong port, wrong bind address, TLS failure, or a
reverse proxy breaking the stream. **It is never an authentication or tool
problem** — those return a protocol error instead.

This is the same fault that produced `HTTP 0 from Playwright service` in
`MOMI-1791564644812`. That run's step 0 was:

```json
{"step":0,"action":"launch","success":false,"http_status":0,"error":"HTTP 0 from Playwright service"}
```

`http_status: 0` means no HTTP response ever arrived. The MCP node below it could
not reach the browser service. Fix this and the `1_pelicargo` failure goes away.

---

## 1. The 60-second test — do this first

Run it **from inside the n8n container**. Testing from the host proves nothing,
because the host is not where the connection is made.

```bash
# find the container name
docker ps --format '{{.Names}}\t{{.Image}}'

# DNS + TCP reachability
docker exec -it <n8n-container> sh -c "nc -zv host.docker.internal 8931"

# HTTP reachability (substitute your real port)
docker exec -it <n8n-container> sh -c \
  "curl -sS -m 5 -o /dev/null -w 'http_code=%{http_code}\n' http://host.docker.internal:8931/mcp"
```

Interpretation:

| Result | Meaning | Go to |
|--------|---------|-------|
| `http_code=000` or `nc` fails | Nothing is listening / no route | §2 or §3 |
| `http_code=200/307/401/406` | Reachable. The port is fine. | §4 (transport mismatch) |
| `could not resolve host` | `host.docker.internal` not defined (Linux) | §3 |

---

## 2. Bind address — the most common cause

`@playwright/mcp` binds to **`localhost` / `127.0.0.1` by default**. A listener on
loopback is unreachable from any container, no matter what URL you configure.

```bash
# WRONG - only reachable from the host itself
npx @playwright/mcp@latest --headless --port 8931

# RIGHT - reachable from the Docker network
npx @playwright/mcp@latest --headless --port 8931 --host 0.0.0.0
```

Confirm on the host that it is listening on all interfaces, not just loopback:

```powershell
# Windows
netstat -ano | findstr :8931
# you want 0.0.0.0:8931  (not 127.0.0.1:8931)
```

```bash
# Linux / macOS
ss -ltnp | grep 8931
```

Security note: `--host 0.0.0.0` exposes an unauthenticated browser to your LAN.
The Playwright MCP CLI has no token or TLS option — put it behind a reverse proxy
with auth, or keep it on a host-only network.

---

## 3. The URL inside n8n — `localhost` means "this container"

Inside a container, `localhost` is the container itself. This is the single most
common `fetch failed` cause in Docker n8n setups.

Use, in order of preference:

| Where the MCP server runs | URL to put in the node |
|---------------------------|------------------------|
| On the Docker host | `http://host.docker.internal:8931/mcp` |
| Host, and `host.docker.internal` will not resolve | `http://172.17.0.1:8931/mcp` |
| Another container on the same compose network | `http://<service-name>:8931/mcp` |

Your orchestrator already reaches the host this way (`whatsapp_api_url` is
`http://host.docker.internal:8080`), so `host.docker.internal` does resolve in
your setup — use it.

**On Linux only**, `host.docker.internal` does not exist by default. Add it:

```yaml
services:
  n8n:
    image: docker.n8n.io/n8nio/n8n
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - N8N_HOST=0.0.0.0
```

or with `docker run`: `--add-host host.docker.internal:host-gateway`.

If it still fails on Windows/macOS Docker Desktop, check Windows Firewall — it
frequently blocks the Docker bridge subnet from reaching host ports.

---

## 4. Transport and endpoint must match

The `MCP Client` node has **Server Transport** and **MCP Endpoint URL** as
separate parameters. They must agree:

| Server Transport | Endpoint URL |
|------------------|--------------|
| HTTP Streamable  | `.../mcp`    |
| SSE              | `.../sse`    |

`@playwright/mcp` serves **both** — `/mcp` (streamable HTTP, current) and `/sse`
(legacy). Pointing a streamable-HTTP transport at `/sse`, or an SSE transport at
`/mcp`, fails with `fetch failed` even though the port is perfectly reachable.

Also set **Authentication** to *None* unless you actually put the server behind
auth — a client expecting a bearer token against an open server fails the same
way.

---

## 5. Two environment traps

### SSRF protection (n8n 2.12+)

Off by default. If you enabled it with `N8N_SSRF_PROTECTION_ENABLED=true`, it
blocks private ranges by default — and `host.docker.internal` resolves to
`172.17.0.1`, which is inside `172.16.0.0/12`. Allow it explicitly:

```yaml
environment:
  - N8N_SSRF_ALLOWED_HOSTNAMES=host.docker.internal
  - N8N_SSRF_ALLOWED_IP_RANGES=172.16.0.0/12,192.168.0.0/16,10.0.0.0/8
```

Hostname allowlists are checked before IP ranges.

### Reverse proxy / gzip

If the MCP server sits behind nginx, Traefik or Caddy, **gzip and response
buffering break SSE** and produce exactly this error. For the MCP path:

```nginx
location /mcp {
    proxy_pass http://host.docker.internal:8931;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    gzip off;
    chunked_transfer_encoding off;
}
```

---

## 6. Verify before re-running MOMI

```bash
# from inside the n8n container - expect a non-000 status
docker exec -it <n8n-container> sh -c \
  "curl -sS -m 5 -o /dev/null -w '%{http_code}\n' http://host.docker.internal:8931/mcp"

# then, on the host, confirm the server is up and lists its tools
npx @modelcontextprotocol/inspector http://host.docker.internal:8931/mcp
```

Once the node connects, the `Tool` dropdown in the MCP Client node populates
automatically. **If the dropdown stays empty, the node still cannot connect** —
go back to §1.

Then re-run MOMI. `1_pelicargo` should now complete, and `1_efc` will pause for
approval rather than being blocked.

---

## Still failing?

Paste these three things:

1. The **MCP Endpoint URL** and **Server Transport** selected in the `Playwright
   Browser (MCP)` node.
2. The exact command you start the Playwright MCP server with, and its startup
   banner.
3. The output of the §1 `curl` run from inside the container.

Also worth confirming: your orchestrator's `playwright_service_url` defaults to
port **8766**, but the MCP server default above is **8931**. If the sub-workflow
and the MCP node disagree on the port, `fetch failed` is guaranteed.
