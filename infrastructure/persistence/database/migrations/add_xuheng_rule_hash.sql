-- Separate forward migration also supports databases opened during development.
ALTER TABLE xh_rule_sets ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
