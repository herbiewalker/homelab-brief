-- Firmware read from the "<mac>,<model>-<firmware>" token that UniFi switches, access points and PDUs put in
-- front of every syslog line (Task 20). The newest sighting per sender, written once per ingest cycle. The
-- UniFi collector uses it when the Network API cannot be reached.
CREATE TABLE device_header (
  host     TEXT PRIMARY KEY,
  mac      TEXT NOT NULL,
  model    TEXT NOT NULL,
  firmware TEXT NOT NULL,
  seen     TEXT NOT NULL
);
