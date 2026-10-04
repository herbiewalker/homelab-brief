# Log samples

- `raw/` holds real log lines pulled from the syslog share. It is gitignored and never committed.
- `sanitized/` holds the processed copies that the code and tests use. This is the only folder that is committed.

## What the sanitizer must replace

- Hostnames and device names -> stable placeholders such as `host-01`, `host-02` (same input always maps to the same placeholder).
- IPv4 and IPv6 addresses -> the documentation ranges (`198.51.100.x` for private addresses, `203.0.113.x` for public ones, `2001:db8::x` for IPv6), so the commit guard accepts them. Loopback, `0.0.0.0`, and netmasks are kept.
- MAC addresses -> the RFC 7042 documentation range `00:00:5e:00:53:xx`, in the same style (separator and case) as the original.
- Anything that looks like a token, password, API key, or serial number -> replaced with `[REDACTED]` and listed for manual review, never copied through.
- Camera names, usernames, and email addresses -> `camera-NN`, `user-NN`, `email-NN@example.com`.
- DNS domains in coredns lines -> `domain-NN.example` (per-client DNS activity never goes in git).
- Your own private words (site codes and similar) -> `SITE`.

Keep the structure of every line (timestamp, priority, program name, message layout) intact, because the parsers depend on it. The `<mac>,<model>-<firmware>` header token keeps its model and firmware; only the MAC changes.

## How to produce `sanitized/`

1. `.\scripts\pull-samples.ps1` copies the last lines of each source into `raw/` (read-only against the share).
2. Put your private words in the gitignored `.dev/private-names.txt` (4+ characters, any case) and `.dev/sanitizer-words.txt` (shorter words, matched exactly). The commit guard reads the first file too.
3. In WSL: `python3 scripts/sanitize_samples.py`. It reads `raw/`, writes `sanitized/syslog-<placeholder-ip>.log`, keeps its mapping in `.dev/sanitizer-map.json` (so the same value always gets the same placeholder), and writes a report with no raw values to `.dev/sanitize-report.txt`.
4. Before anything is written it checks that no replaced value survives anywhere and that the commit guard finds nothing; if either fails it writes nothing.
5. **Read the output before committing.** The check cannot find a private name it was never taught. A quick way is to list the rarest words in the output and look for names; add any you find to the private word files and re-run.

## Coverage to collect (about 50 lines each)

UDM Pro, UCG-Max, USW switches, USP-PDU-Pro, UNVR/Protect (CEF), each Unraid server, and any other source that appears in the share. Include a few lines that look like errors and a few that the ignore list should suppress.

Do not commit anything from `raw/`. Before the first push, run `git status` and confirm only `sanitized/` content is staged.
