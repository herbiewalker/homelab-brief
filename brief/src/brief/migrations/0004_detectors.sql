-- Detector inputs (Task 8).

-- How far a source's event times are from the time Vector received them (seconds, event minus
-- receive; positive means the device's clock is ahead). The ingest cycle writes the median of the
-- cycle's live events, one UPDATE per host per cycle, so this never grows. Imported history is
-- excluded because its receive time is the import, not the event.
ALTER TABLE host ADD COLUMN clock_skew_s REAL;
ALTER TABLE host ADD COLUMN skew_updated TEXT;
