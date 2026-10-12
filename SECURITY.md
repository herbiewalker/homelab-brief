# Security policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue: use GitHub's "Report a vulnerability" button on the Security tab of this repository. Include the version (`/healthz` shows it), what you did, and what you saw. Do not include real logs, addresses, or keys; `brief support-bundle` makes a report that has been scrubbed of those.

This is a one-maintainer project, so expect an acknowledgement within about a week, not hours. Fixes ship as a normal release; the changelog says when a release fixes a security problem.

## What the product does to protect itself

- `/admin` and every write need a login over HTTPS; there are no default credentials (the first account is made with a one-time token printed in the container log).
- Passwords are hashed with Argon2; sessions are `Secure`, `HttpOnly`, `SameSite=Strict`; every write needs a CSRF token and is recorded in the audit log; failed logins are rate limited.
- The read-only pages, `/api/v1`, and `/metrics` change nothing. The MCP server is read-only, token-authenticated with scopes, and audited.
- Secrets come from container variables, are never echoed into logs or pages, and are scrubbed from support bundles.
- The optional SSH server is off by default, key-only, and has no root login.
- A cloud AI provider only receives system-level facts: never device-level findings, raw log lines, DNS data, or device lists.

## What is out of scope

The container is meant for a trusted LAN. The status pages, `/api/v1`, and `/metrics` are readable without a login by design (put a reverse proxy in front if you need otherwise), and the default TLS certificate is self-signed. Running it on the open internet is not supported.
