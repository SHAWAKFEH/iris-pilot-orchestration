-- Pipeline bookkeeping used to make stale state visible.
--
-- dataset_version.version is bumped by promotion only when core rows actually
-- change, so an unchanged rerun keeps the same version.
-- view_state.source_version records which dataset version a materialized view
-- was last refreshed from. view stale  <=>  source_version < dataset version.

CREATE TABLE ops.dataset_version (
    dataset     text        PRIMARY KEY,
    version     bigint      NOT NULL DEFAULT 0 CHECK (version >= 0),
    changed_at  timestamptz
);

INSERT INTO ops.dataset_version (dataset) VALUES ('bess_site'), ('peatland_parcel');

CREATE TABLE ops.view_state (
    view_name       text        PRIMARY KEY,
    dataset         text        NOT NULL REFERENCES ops.dataset_version (dataset),
    source_version  bigint      NOT NULL,
    row_count       bigint      NOT NULL,
    refreshed_at    timestamptz NOT NULL
);
