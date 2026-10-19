-- The name a device calls itself in its syslog header (the newest one seen), and a name the admin gave it.
-- The system pages list devices by name instead of by address. An alias wins over the name the device sends.
ALTER TABLE host ADD COLUMN name TEXT;
ALTER TABLE host ADD COLUMN name_seen TEXT;  -- when that name was seen, so an older line never renames a device

CREATE TABLE host_alias (
  host    TEXT PRIMARY KEY,
  alias   TEXT NOT NULL,
  updated TEXT NOT NULL
);
