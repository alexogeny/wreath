-- Neutral schema for the workload suite. Generic names only; a conformance
-- adapter renames these externally without changing framework code.

CREATE TABLE IF NOT EXISTS "widget" (
    id    integer PRIMARY KEY,
    value integer NOT NULL
);

CREATE TABLE IF NOT EXISTS "quotation" (
    id      integer PRIMARY KEY,
    message text NOT NULL
);
