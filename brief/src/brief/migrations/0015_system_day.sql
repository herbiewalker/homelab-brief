-- One row per system per day: the worst state the system showed that day (red, yellow, green), and how many
-- times it was sampled. It feeds the 30-day status bars on the Overview and the public status page. A day with no
-- row means nothing was recorded (the container was off), which the bars show as "no data", never as green.
CREATE TABLE system_day (
  day       TEXT    NOT NULL,          -- YYYY-MM-DD (UTC)
  system_id TEXT    NOT NULL,
  worst     TEXT    NOT NULL,          -- red | yellow | green | unknown
  samples   INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (day, system_id)
) WITHOUT ROWID;
