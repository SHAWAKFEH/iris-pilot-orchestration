-- Staging keeps source data as delivered, in WGS84 (EPSG:4326).
-- geom is nullable here on purpose: a source may lack geometry. Promotion
-- refuses such rows and reports them; it never invents a geometry.

CREATE TABLE staging.bess_site_candidate (
    country_code     core.country_code NOT NULL,
    source_id        text           NOT NULL CHECK (source_id <> ''),
    site_name        text           NOT NULL,
    capacity_mw      numeric(8, 2)  CHECK (capacity_mw > 0),
    grid_distance_m  numeric(10, 1) CHECK (grid_distance_m >= 0),
    source_name      text           NOT NULL,
    source_date      date           NOT NULL,
    review_status    text           NOT NULL DEFAULT 'pending'
                     CHECK (review_status IN ('pending', 'accepted', 'rejected')),
    geom             geometry(MultiPolygon, 4326),
    loaded_at        timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, source_id)
);

COMMENT ON COLUMN staging.bess_site_candidate.capacity_mw     IS 'Planned installed capacity, megawatts. NULL = not provided by source.';
COMMENT ON COLUMN staging.bess_site_candidate.grid_distance_m IS 'Distance to nearest grid connection point, metres. NULL = not provided by source.';

CREATE TABLE staging.peatland_parcel_candidate (
    country_code     core.country_code NOT NULL,
    source_id        text           NOT NULL CHECK (source_id <> ''),
    parcel_name      text           NOT NULL,
    peat_depth_m     numeric(5, 2)  CHECK (peat_depth_m >= 0),
    land_use         text           CHECK (land_use IN ('grassland', 'arable', 'forest', 'wetland')),
    source_name      text           NOT NULL,
    source_date      date           NOT NULL,
    review_status    text           NOT NULL DEFAULT 'pending'
                     CHECK (review_status IN ('pending', 'accepted', 'rejected')),
    geom             geometry(MultiPolygon, 4326),
    loaded_at        timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, source_id)
);

COMMENT ON COLUMN staging.peatland_parcel_candidate.peat_depth_m IS 'Mean peat layer depth, metres. NULL = not provided by source.';
