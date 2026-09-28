-- Candidate views: explainable screening on top of promoted core data.
-- Created WITH NO DATA so an unrefreshed view is visibly unpopulated instead
-- of silently empty; the pipeline's refresh stage populates them.
-- Missing inputs produce 'insufficient_data'. Values are never imputed.

CREATE MATERIALIZED VIEW core.mv_bess_candidates AS
WITH measured AS (
    SELECT s.*,
           ST_Area(s.geom)                                AS area_m2_exact,
           ST_Transform(ST_PointOnSurface(s.geom), 4326)  AS point_wgs84
    FROM core.bess_site AS s
)
SELECT
    m.country_code,
    m.site_id,
    m.site_name,
    m.capacity_mw,
    m.grid_distance_m,
    round((m.area_m2_exact / 10000.0)::numeric, 2) AS area_ha,
    round(ST_Y(m.point_wgs84)::numeric, 6)         AS lat,
    round(ST_X(m.point_wgs84)::numeric, 6)         AS lon,
    CASE
        WHEN m.capacity_mw IS NULL OR m.grid_distance_m IS NULL THEN 'insufficient_data'
        WHEN m.area_m2_exact >= 5000 AND m.grid_distance_m <= 2000 THEN 'pass'
        ELSE 'review'
    END AS screening_status,
    coalesce(nullif(concat_ws('; ',
        CASE WHEN m.capacity_mw IS NULL     THEN 'capacity not provided by source' END,
        CASE WHEN m.grid_distance_m IS NULL THEN 'grid connection distance not provided by source' END,
        CASE WHEN m.area_m2_exact < 5000    THEN 'area below 0.5 ha minimum' END,
        CASE WHEN m.grid_distance_m > 2000  THEN 'grid connection farther than 2 km' END
    ), ''), 'meets baseline screening rules') AS screening_notes,
    m.source_name,
    m.source_date,
    m.geom
FROM measured AS m
WITH NO DATA;

CREATE UNIQUE INDEX mv_bess_candidates_pk ON core.mv_bess_candidates (country_code, site_id);


-- 8 eco-points/m2 is the current commercial baseline (see dossier notice),
-- not certified compensation.
CREATE MATERIALIZED VIEW core.mv_peatland_candidates AS
WITH measured AS (
    SELECT p.*,
           ST_Area(p.geom)                                AS area_m2_exact,
           ST_Transform(ST_PointOnSurface(p.geom), 4326)  AS point_wgs84
    FROM core.peatland_parcel AS p
)
SELECT
    m.country_code,
    m.parcel_id,
    m.parcel_name,
    m.peat_depth_m,
    m.land_use,
    round(m.area_m2_exact::numeric, 0)             AS area_m2,
    round((m.area_m2_exact / 10000.0)::numeric, 2) AS area_ha,
    8::numeric                                     AS eco_points_per_m2,
    round(m.area_m2_exact::numeric, 0) * 8         AS eco_points_indicative,
    round(ST_Y(m.point_wgs84)::numeric, 6)         AS lat,
    round(ST_X(m.point_wgs84)::numeric, 6)         AS lon,
    CASE
        WHEN m.peat_depth_m IS NULL OR m.land_use IS NULL THEN 'insufficient_data'
        WHEN m.peat_depth_m >= 0.30 AND m.land_use IN ('grassland', 'arable') THEN 'pass'
        ELSE 'review'
    END AS screening_status,
    coalesce(nullif(concat_ws('; ',
        CASE WHEN m.peat_depth_m IS NULL THEN 'peat depth not provided by source' END,
        CASE WHEN m.land_use IS NULL     THEN 'land use not provided by source' END,
        CASE WHEN m.peat_depth_m < 0.30  THEN 'peat layer thinner than 0.30 m' END,
        CASE WHEN m.land_use NOT IN ('grassland', 'arable')
             THEN 'land use ''' || m.land_use || ''' is not drained agriculture; rewetting uplift unclear' END
    ), ''), 'meets baseline screening rules') AS screening_notes,
    m.source_name,
    m.source_date,
    m.geom
FROM measured AS m
WITH NO DATA;

CREATE UNIQUE INDEX mv_peatland_candidates_pk ON core.mv_peatland_candidates (country_code, parcel_id);
