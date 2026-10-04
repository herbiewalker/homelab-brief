# Network map

Goal: a diagram of servers, network equipment, and services that you can drag around, with live status colors, so you get a holistic view of the homelab in one place. It comes in three forms from one data file:

| Output | For | How |
| --- | --- | --- |
| `www/map.html` (interactive) | Admin GUI (Network map page), computer, newer tablet, HA webpage card | Cytoscape.js (MIT), vendored and pinned, no CDN, reads `/api/v1/map` |
| `www/map.svg` (static) | Oldest tablets, HA picture card, e-reader | Drawn by Python from the saved layout; no browser needed |
| `out/map.drawio` (optional) | Editing or documenting in diagrams.net | Plain XML export, off by default |

The map is optional and switched on in `brief/map.yaml`. It works with only the manual topology file, and gets richer as the Phase 2 collectors come online.

## Where the data comes from

- **UniFi inventory (Task 20):** gateways, switches, APs, cameras, with each device's uplink (parent device, port, speed when known), model, firmware, and update status.
- **Unraid inventory (Task 19):** each server's containers and VMs.
- **`brief/topology.yaml` (manual, private):** things collectors cannot discover (ISP, UPS, Home Assistant, tablets), manual edges (power, dependencies), and overrides (labels, hiding). Manual entries always win.
- **`brief/systems.yaml`:** grouping, so map nodes and dashboard tiles agree.
- **`status.json` and open findings:** node colors and badges.

Discovered and manual data are merged into two tables, then exported as `map.json`:

```sql
CREATE TABLE topo_node (
  node_id TEXT PRIMARY KEY, kind TEXT, label TEXT, parent TEXT,  -- parent: server for a container
  system_id TEXT, source TEXT,                                   -- discovered | manual
  attrs_json TEXT, first_seen TEXT, last_seen TEXT
);
CREATE TABLE topo_edge (
  src TEXT, dst TEXT, kind TEXT,                                 -- uplink | hosts | depends | power
  attrs_json TEXT, source TEXT, last_seen TEXT,
  PRIMARY KEY (src, dst, kind)
);
```

A discovered infrastructure node that has never been seen before is added with a "new" badge and also raises a `new_infrastructure` finding.

## What the map shows

- **Node kinds:** internet, gateway, switch, AP, server, VM, container, service, camera, IoT group, client group, UPS, display, other.
- **Edge kinds:** uplink (port and speed in the tooltip when known), hosts (server to container or VM), depends (manual), power (manual).
- **Clients are grouped per VLAN** with a count by default, which keeps the map readable and protects privacy. Devices with the `ignore` profile never appear. `security_only` devices only count toward their group unless a finding is open for them.
- **Status overlay:** node color follows its system state (green, yellow, red, or grey for no data); badges show open findings, pending firmware, or a disk bucket.
- **Layers:** network, services, and power can be toggled.
- **Detail panel:** clicking a node shows model, firmware, update pending, last seen, open findings, and a link to that host's detail page in the brief.
- **Servers are compound nodes:** containers and VMs sit inside their server and can be collapsed.

## Moving things and saving the layout

- Drag any node. Positions are saved in that browser automatically.
- **Export layout** downloads `map_layout.json`; **Import layout** loads one. In the admin GUI, **Save as default** stores the layout on the server (logged in the audit log), making it the default for every screen and for the static SVG. The read-only page never writes to the server.
- Nodes without a saved position are placed by the automatic layout set in `map.yaml`, and only those nodes move. Your arrangement is never reshuffled.
- Saving a default layout requires the admin login; the read-only map adds no write path.

## Efficiency

- `map.json` is regenerated on the hourly light run only if inventory or status changed, and fully on the daily run.
- The interactive page re-fetches `map.json` every `refresh_s` seconds (default 300). There are no websockets and no external requests.
- This page is the one exception to the no-JavaScript rule. The library is vendored, pinned, and license-recorded, and there is no build step.
- Test the interactive page on the oldest tablet. If it is sluggish, point that tablet at `map.svg` instead.

## Privacy

`map.json`, `map.svg`, `map.drawio`, `topology.yaml`, and `map_layout.json` describe your network. They are generated or private, never committed, and served only on the LAN like the rest of `/www`.

## Done when

- With collectors disabled and only `topology.yaml` filled in, the map renders and nodes can be dragged.
- A dragged layout survives a page reload, and exporting then importing it restores the same arrangement on another device.
- With `map_layout.json` on the server, `map.svg` matches the saved arrangement.
- A newly discovered switch appears with a "new" badge, auto-placed without moving existing nodes, and raises a `new_infrastructure` finding.
- Devices with the `ignore` profile never appear, and clients appear as per-VLAN groups.
- Node colors change when a system's state changes in `status.json`.
