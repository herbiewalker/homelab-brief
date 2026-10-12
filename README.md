<p align="center"><img src="deploy/icon.png" alt="Homelab Brief icon" width="120"></p>

# Homelab Brief

One container that collects your homelab's logs and API data, learns what normal looks like, and turns it into a daily news-style brief, alerts that don't nag, and live status for Home Assistant and any other dashboard. Built for Unraid, runs anywhere Docker runs.

![The Overview page: status banner, trend tiles, systems with 30-day bars, incident timeline](docs/ui-audit/overview-1280-dark.jpg)

## What it does

- **Collects** syslog straight from your devices (the container gets its own LAN address and listens on port 514), plus the Unraid, UniFi, SNMP, iDRAC, and Home Assistant (power) APIs.
- **Learns** a baseline for every log pattern and host, so "new", "much more than usual", and "went silent" are findings instead of noise.
- **Publishes** a daily brief and weekly report, MQTT sensors with Home Assistant discovery, Prometheus `/metrics`, a read-only REST API, static pages for tablets and e-readers, and optional push notifications through Apprise.
- **Is administered** from a web console (HTTPS, login, audit log, every setting a control, versioned config with rollback), with a full `brief` command line and optional key-only SSH.
- **Works without AI.** Optionally add any OpenAI-compatible model server (a local GPU box or a hosted service) for plain-language explanations and a few read-only agents, plus a read-only MCP server so an assistant can query it. AI never decides, never acts, and never receives device-level data when it is a cloud model.
- **Is light:** one image, about 770 MB of memory at most, batched writes, no headless browser.

## Status: early, one maintainer, honest about it

This is version 0.0.x software. It was built in small, tested steps (over 1,800 automated tests, an end-to-end suite against the real image, and an upgrade test between releases), and it runs on the author's own staging server. What that does and does not mean:

- **Proven live:** syslog ingest and baselines on real history, the admin console, the Unraid collector against a real server, backups, and in-place upgrades.
- **Proven against fakes only:** the UniFi, SNMP, iDRAC, and Home Assistant collectors (tested against simulated devices that mimic the real APIs), SNMPv3, and the UniFi client and traffic features. Expect to meet a real-device surprise.
- **Lightly exercised:** the AI layer and agents (one small local model and scripted models), and the MCP server (no real MCP client yet).
- **Not production-hardened:** there is no multi-user model (one admin), no single sign-on, and only `amd64` images.

Treat it as a homelab tool to try next to your real setup, not as something your security depends on yet. The changelog (`CHANGELOG.md`) lists every change; open an issue if something doesn't match what you see.

## Install

You need Docker, a place to keep four folders, and (for the best result) a spare IP address on your LAN so devices can send syslog to the container directly.

**On Unraid (Docker tab, one-click updates):** run this once in the Unraid terminal, then open the Docker tab, click Add Container, pick `homelab-brief` from the Template dropdown, set a fixed IP on `br0`, and apply:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-homelab-brief.xml   https://raw.githubusercontent.com/herbiewalker/homelab-brief/main/deploy/unraid-template.xml
```

(No terminal? Download that file and save it as `my-homelab-brief.xml` in the `config/plugins/dockerMan/templates-user` folder of the flash share, from any computer.) A Community Applications listing, which would make it a search-and-click install, is planned; see `docs/DECISIONS.md`.

The image follows the `stable` tag, so the Docker tab offers **Update** when a release is out. Each start keeps a copy of the database before it migrates.

**With Docker Compose or `docker run`:** the three reference files in [`deploy/`](deploy/) cover an own-IP macvlan network, host networking, and a plain bridge with published ports. The quickest way to look around (bridge mode, ports moved off the standard ones):

```bash
docker run -d --name homelab-brief --restart unless-stopped \
  -p 5514:514/udp -p 5514:514/tcp -p 8480:80 -p 8443:443 \
  -e HTTPS_PUBLIC_PORT=8443 \
  -v $PWD/hb/config:/config -v $PWD/hb/state:/state \
  -v $PWD/hb/archive:/archive -v $PWD/hb/out:/out \
  ghcr.io/herbiewalker/homelab-brief:stable
```

Then open `https://<host>:8443/admin` (the certificate is self-signed), read the one-time setup token from `docker logs homelab-brief`, create the admin account, and follow the setup wizard. Point one device's syslog at `<host>:5514` and watch it appear. Bridge mode can hide each sender's address (Docker's proxy replaces it), and the Network page tells you when it sees that; an own IP is the better setup. Full step-by-step instructions, including MQTT and Home Assistant, are in [`deploy/README.md`](deploy/README.md).

The image is public: `ghcr.io/herbiewalker/homelab-brief:stable` (follows releases) or an exact version such as `ghcr.io/herbiewalker/homelab-brief:0.0.57`. There is no `latest` tag.

## Documentation

| Read | For |
| --- | --- |
| [`deploy/README.md`](deploy/README.md) | Installing, upgrading, backing up, rolling back |
| [`docs/project-plan.md`](docs/project-plan.md) | What it is for, the outputs, the phases |
| [`docs/unraid-backend-guide.md`](docs/unraid-backend-guide.md) and [`docs/guide/`](docs/guide/) | The technical guide: collection, baselines, the brief, publishing, operations, collectors, the map, access, AI, agents, the console |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why things are the way they are, and what is still open |
| [`CHANGELOG.md`](CHANGELOG.md) | Every release |

## Privacy and security in one paragraph

Everything runs on your network; nothing phones home and the pipeline needs no internet. The admin console needs a login over HTTPS, every write is protected against cross-site requests and recorded in an audit log, and passwords are hashed with Argon2. Per-device activity stays local: a cloud AI provider only ever sees system-level facts. Secrets come from container variables, never from files in the repository. To report a vulnerability, see [`SECURITY.md`](SECURITY.md).

## Contributing

Issues and pull requests are welcome; start with [`CONTRIBUTING.md`](CONTRIBUTING.md). The one hard rule: this repository holds the generic product only, so no real hostnames, addresses, MACs, names, or log lines, ever. A commit guard and CI enforce it.

## Licence

[GNU Affero General Public License v3.0](LICENSE). If you run a modified version for others over a network, the AGPL asks you to offer them your modified source; the console footer links to this repository for that reason. The vendored front-end files (htmx, Alpine.js, Cytoscape.js) and the other components that ship in the image keep their own permissive licences, listed in [`brief/src/brief/static/vendor/VENDOR.md`](brief/src/brief/static/vendor/VENDOR.md) and in `docs/DECISIONS.md`.
