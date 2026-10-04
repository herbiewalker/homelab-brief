# Admin GUI, API, and command-line access

Goal: administer the whole product from a modern browser GUI, with a command line (and optional SSH) for anything you prefer to type, and standard interfaces for every dashboard.

## Two kinds of web access

| Path | Protocol | Login | For |
| --- | --- | --- | --- |
| `/` read-only views: brief, status board PNG, static map, `status.json` | HTTP on the LAN | None | Old tablets, HA webpage and picture cards, e-reader |
| `/api/v1/...` read-only JSON | HTTP or HTTPS on the LAN | None for reads | Dashboards and scripts |
| `/metrics` | HTTP on the LAN | None | Prometheus-format scrapers (Grafana and others) |
| `/admin` GUI and every write | HTTPS only | Required | You |
| `/mcp` | HTTPS | Bearer token with scopes | AI clients (read-only) |

Old tablets struggle with self-signed certificates, which is why the read-only views stay on plain HTTP. Nothing on the HTTP side can change anything.

## Security model

The code is `brief/src/brief/web.py` (the apps), `auth.py` (accounts, sessions, throttling), `tlscert.py`, and `serve.py`. `scripts/e2e_web.py` checks all of it against the built image over real TLS.

- **Two apps, one codebase.** The HTTP app holds only the read-only routes, so the admin area does not exist there: `GET /admin` is redirected to HTTPS, and a write to it is refused unprocessed. The HTTPS app adds `/admin`. Ports come from `brief/network.yaml` (`ports.http`, `ports.https`), with `HTTP_PORT`, `HTTPS_PORT`, and `HTTPS_PUBLIC_PORT` (the port a browser should use when the template maps a different one) as environment overrides.
- **No default credentials.** The first admin is created either with `docker exec -it homelab-brief brief admin set-password`, or on `/admin/setup` with a one-time token printed to the container log as a `setup_token` line. The token is stored only as a hash, expires after 24 hours, is replaced at every start while no admin exists, and works once. A weak password does not use it up.
- **Passwords** are hashed with Argon2id, at least 12 characters (length matters, there are no composition rules), and are never logged or stored in the clear.
- **Sessions** are server-side. The cookie (`__Host-brief_session`: Secure, HttpOnly, SameSite=Strict, no domain, path `/`) holds a random 256-bit token; only its SHA-256 is stored. A session ends after 30 minutes idle or 12 hours in all, on sign-out, and on any password change.
- **Default-deny guard.** One dependency on the whole `/admin` router requires a session for every route except sign-in and setup, and requires a CSRF token for every write (any method but GET, HEAD, OPTIONS). A route added later is protected without anyone remembering to opt in. A test walks the router to prove it, and another proves nothing is registered under `/admin` outside it.
- **CSRF protection** uses a token bound to the session (a hidden form field, or an `X-CSRF-Token` header for scripts), a double-submit cookie before sign-in, and an Origin or Referer check that must match this site's scheme and host.
- **Failed logins are throttled** per address and per username (5 free failures in 15 minutes, then a lock of 30 seconds that doubles up to 15 minutes). The failures live in the database, so a restart does not reset them, even the right password waits out a lock, and an unknown username answers exactly like a wrong password. Only the direct peer address is trusted (no proxy headers).
- **TLS** uses a self-signed certificate generated on first start that names the advertised address, this host, and `localhost`, and is regenerated when those change or it nears expiry. Put `cert.pem` and `key.pem` in `/config/tls` to use your own (or put the GUI behind your existing reverse proxy); a certificate you supply is never overwritten. There is no HSTS, because a self-signed certificate that changes would lock people out.
- **Headers:** every response sends a strict Content-Security-Policy (no scripts at all today), nosniff, frame denial, and no referrer, and admin pages are `no-store`.
- **Audit log.** Every sign-in, failed sign-in, throttled attempt, sign-out, password change, failed setup, and account creation is recorded with time, user, and address (never a password).
- **LAN only.** Do not route the admin GUI through a Cloudflare tunnel or any public reverse proxy. For remote administration use a VPN (Tailscale, for example).
- **Later:** single sign-on (OIDC) and an optional TOTP second factor.

### Health

`/healthz` (HTTP, no login) returns 200 or 503. It checks that the database is readable, that the worker has completed a cycle recently (it records an `ingest.cycle_seconds` metric each cycle, with a grace period after start), and that the syslog receiver accepts connections. Docker's health check uses it, so a crashed worker or Vector now shows as unhealthy. The status page at `/` shows the same checks with the last 24 hours of ingest and the learning banner, with no JavaScript, for old tablets.

## GUI

Server-rendered pages with htmx and Alpine.js (vendored, pinned, no build step), responsive, with dark and light themes. It is fast on a desktop browser and needs no Node toolchain. The tablet views stay JavaScript-free.

| Page | What you can do |
| --- | --- |
| Overview | Health, version, ingest rate per source, last hourly/daily/weekly runs, open errors |
| Sources | Every sending host, last line received, receive rate, parse-failure rate, unknown hosts to map into `systems.yaml` |
| Collectors | Enable or disable, test the connection, last success, last error, metrics produced |
| AI | Providers (test connection, list models, latency), task chains, prompt editor with history, a side-by-side preview of template versus AI output, the call log, and MCP client tokens with scopes (see `09-ai-integration.md`) |
| Findings and alerts | Current findings by rank; acknowledge, snooze, or "ignore this signature" (writes the ignore rule for you) |
| Replay and tuning | Pick a date range, run a dry-run replay with the current config, see the findings and pushes it would produce |
| Config | Edit any `brief/*.yaml` with schema validation, diff against the current version, save, and roll back from history |
| Systems, devices, topology | Form-based editors for `systems.yaml`, `devices.yaml`, and `topology.yaml` |
| Log search | Search the raw archive by host, time range, and text |
| Network | Detected network mode, addresses, and ports; advertised address and port settings; warnings; a live syslog test; per-source repointing status; the exact Unraid template settings for each mode (see below) |
| Network map | The interactive map, with layout save and export |
| Backups | Download or restore an appdata backup; export history |
| Audit log | Every admin change |

Config edits are versioned: each save keeps the previous version, and rollback is one click. An edit that fails validation cannot be saved.

## Network page

Docker decides the container's network mode and IP from the Unraid template; nothing inside the container can change them. The Network page handles everything around that, stored in `brief/network.yaml`:

- **Detects the mode** (own IP on br0 with ipvlan or macvlan, host, default bridge, or a user-defined bridge) and shows the container's addresses, the host's address, and which ports each process listens on.
- **Advertised address:** the IP or hostname devices send syslog to and dashboards use. It feeds HA discovery links, alert links, map links, and the TLS certificate names (the certificate is regenerated when it changes).
- **Ports:** syslog, HTTP, HTTPS, and SSH listen ports. Saving restarts only the affected processes inside the container. In bridge mode the template must map the same ports, and the page shows that mapping.
- **Warnings, each with the fix:**
  - *Sender IPs lost:* if every source appears to come from one Docker gateway address (for example `172.17.0.1`), the bridge is rewriting source addresses and host identity breaks. Switch to own IP or host mode.
  - *Port clash in host mode:* Unraid's web UI uses 80/443, and its syslog server uses 514 when enabled. Use the suggested 8480, 8443, and 5514, or turn Unraid's syslog server off.
  - *Host cannot reach its own container:* in own-IP mode, the host server's own syslog needs "host access to custom networks" enabled.
- **Syslog test:** shows the exact `logger` command for any server and turns green when the test line arrives.
- **Repointing status:** every expected source from `systems.yaml` with the address it should send to and when its last line arrived.
- **Template settings for each mode:** a copyable summary of the Unraid template fields (network type, fixed IP, port mappings) to change the mode yourself in Unraid's Docker page. The container never changes its own network, so a wrong setting cannot cut off the GUI you are using.

| Mode | Devices send to | The host server's own syslog | Ports | Notes |
| --- | --- | --- | --- | --- |
| Own IP (br0, ipvlan or macvlan) | Container IP:514 | Needs "host access to custom networks" | Standard (514, 80, 443) | Recommended; cleanest separation |
| Host | The host server IP:5514 (or 514 with Unraid's syslog server off) | Works without extra settings | Must avoid Unraid's 80/443 (use 8480/8443) | Simple; shares the host server's address |
| Bridge (default or user-defined) | The host server IP:mapped port | Works | Mapped in the template | Check that real sender IPs arrive (warning above) |

## Command line

The `brief` command covers everything the GUI does:

```
brief status
brief replay --from 2026-10-01 --to 2026-10-07 --dry-run
brief export --host udm --sig 1042 --days 30
brief weekly
brief config validate
brief config rollback <file> <version>
brief network show
brief network test --from <server-ip>
brief collector test unraid
brief admin set-password
brief backup create
```

**Default way in (no extra services):** SSH to the Unraid host, then

```
docker exec -it homelab-brief brief status
```

or use the container console in the Unraid web UI.

**Optional built-in SSH server** (disabled by default, for direct access to the container's own IP):

- Key-only. Put public keys in `appdata/homelab-brief/ssh/authorized_keys`. Password login is refused.
- No root login. You land as a dedicated `brief` user with the CLI available.
- Listens only on the container's own IP, port 22, LAN only.
- Enable it with `SSH_ENABLED=true` in the template.

## Standard interfaces out

| Interface | Standard | Used by |
| --- | --- | --- |
| MQTT with Home Assistant discovery | MQTT 3.1.1/5 | Home Assistant sensors, alerts, feedback buttons |
| `/metrics` | Prometheus / OpenMetrics text format | Grafana or any scraper |
| `/api/v1` | Versioned REST, JSON | Any dashboard or script |
| Static files | HTML, PNG, SVG, JSON | Tablets, HA cards, e-reader |
| `/mcp` | Model Context Protocol (Streamable HTTP), read-only, token scopes | Home Assistant's MCP client, Claude, and other AI assistants |
| Apprise (optional) | Apprise notification URLs | ntfy, Gotify, email, and many others |

## Done when

- `/admin` cannot be reached without logging in, and a fresh install has no default password.
- Every config file can be edited, validated, saved, and rolled back from the GUI, and each change appears in the audit log.
- Every GUI action has a CLI equivalent that works through `docker exec`.
- With SSH enabled, password login is refused and key login works.
- Read-only views still load on the oldest tablet without a certificate prompt.
