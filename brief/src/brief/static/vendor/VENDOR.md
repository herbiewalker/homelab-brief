# Vendored front-end files

Served from our own origin (the admin pages make no outside requests, and there is no build step).
Pinned exactly; to update, replace the file, change the name here and in `templates/admin_layout.html`,
and record the new version and license in `docs/DECISIONS.md`.

| File | Project | Version | License | Source | sha256 |
| --- | --- | --- | --- | --- | --- |
| `htmx-2.0.11.min.js` | htmx | 2.0.11 | 0BSD | npm `htmx.org`, `dist/htmx.min.js` | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| `alpine-csp-3.17.4.min.js` | Alpine.js (CSP build) | 3.17.4 | MIT | npm `@alpinejs/csp`, `dist/cdn.min.js` | `0d18d7f8d7910e2e0212f0f056b12f50bebc3abb7d88d2f7c7cb4c336fe4519a` |
| `cytoscape-3.34.3.min.js` | Cytoscape.js (network map) | 3.34.3 | MIT | npm `cytoscape`, `dist/cytoscape.min.js` | `5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91` |

The CSP build of Alpine is used because the standard build evaluates expressions with `eval`, which the
admin pages' Content-Security-Policy forbids. Components are registered in `../admin.js` instead of
being written inline. htmx runs with `allowEval` off.
