-- Core entities are stored in EPSG:3035 (ETRS89 / LAEA Europe): an
-- equal-area projection in metres, so ST_Area() returns true square metres.
-- Identifiers are country-scoped: the primary key is (country_code, id).

CREATE TABLE core.bess_site (
    country_code     core.country_code NOT NULL,
    site_id          text           NOT NULL,
    site_name        text           NOT NULL,
    capacity_mw      numeric(8, 2),
    grid_distance_m  numeric(10, 1),
    source_name      text           NOT NULL,
    source_date      date           NOT NULL,
    geom             geometry(MultiPolygon, 3035) NOT NULL CHECK (ST_IsValid(geom)),
    promoted_at      timestamptz    NOT NULL DEFAULT now(),
    updated_at       timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, site_id)
);
CREATE INDEX bess_site_geom_idx ON core.bess_site USING gist (geom);

CREATE TABLE core.peatland_parcel (
    country_code     core.country_code NOT NULL,
    parcel_id        text           NOT NULL,
    parcel_name      text           NOT NULL,
    peat_depth_m     numeric(5, 2),
    land_use         text,
    source_name      text           NOT NULL,
    source_date      date           NOT NULL,
    geom             geometry(MultiPolygon, 3035) NOT NULL CHECK (ST_IsValid(geom)),
    promoted_at      timestamptz    NOT NULL DEFAULT now(),
    updated_at       timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, parcel_id)
);
CREATE INDEX peatland_parcel_geom_idx ON core.peatland_parcel USING gist (geom);
