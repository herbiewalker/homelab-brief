-- Phase 2 tables (Task 18). Collectors write here; the framework itself needs none of them (a collector's
-- state and last good metrics live in `meta`, its numeric history in `metric`), so an install with every
-- collector off just has empty tables.
-- Roll client_hourly, dns_hourly and power_reading up to daily rows after 90 days (retention.yaml).

CREATE TABLE disk (
  disk_id    TEXT PRIMARY KEY,
  server     TEXT,
  model      TEXT,
  serial     TEXT,
  role       TEXT,
  first_seen TEXT
);

CREATE TABLE smart_reading (
  ts      TEXT NOT NULL,
  disk_id TEXT NOT NULL,
  attr    TEXT NOT NULL,
  value   INTEGER,
  worst   INTEGER,
  raw     INTEGER,
  PRIMARY KEY (ts, disk_id, attr)
);
CREATE INDEX smart_reading_disk ON smart_reading (disk_id, attr, ts);

CREATE TABLE inventory (
  item_id           TEXT PRIMARY KEY,
  kind              TEXT,
  name              TEXT,
  model             TEXT,
  current_version   TEXT,
  available_version TEXT,
  checked_at        TEXT
);

CREATE TABLE client (
  mac        TEXT PRIMARY KEY,
  first_seen TEXT,
  last_seen  TEXT,
  hostname   TEXT,
  vendor     TEXT,
  vlan       TEXT
);

CREATE TABLE client_hourly (
  day        TEXT NOT NULL,
  hour       INTEGER NOT NULL,
  mac        TEXT NOT NULL,
  bytes_up   INTEGER,
  bytes_down INTEGER,
  online_min INTEGER,
  PRIMARY KEY (day, hour, mac)
);

CREATE TABLE dns_hourly (
  day         TEXT NOT NULL,
  hour        INTEGER NOT NULL,
  client      TEXT NOT NULL,
  queries     INTEGER,
  blocked     INTEGER,
  top_domains TEXT,
  PRIMARY KEY (day, hour, client)
);

CREATE TABLE power_reading (
  ts        TEXT NOT NULL,
  entity    TEXT NOT NULL,
  system_id TEXT,
  watts     REAL,
  kwh       REAL,
  PRIMARY KEY (ts, entity)
);

-- The network map (docs/guide/07-network-map.md): what a collector saw and how it is connected.
-- parent is the server for a container; source is discovered or manual.
CREATE TABLE topo_node (
  node_id    TEXT PRIMARY KEY,
  kind       TEXT,
  label      TEXT,
  parent     TEXT,
  system_id  TEXT,
  source     TEXT,
  attrs_json TEXT,
  first_seen TEXT,
  last_seen  TEXT
);

-- kind is uplink, hosts, depends, or power
CREATE TABLE topo_edge (
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  attrs_json TEXT,
  source     TEXT,
  last_seen  TEXT,
  PRIMARY KEY (src, dst, kind)
);
