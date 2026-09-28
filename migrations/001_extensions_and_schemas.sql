-- PostGIS provides geometry types, spatial indexes and spatial functions.
CREATE EXTENSION IF NOT EXISTS postgis;

-- staging: raw candidate rows plus a review decision (pending/accepted/rejected).
-- core:    promoted, validated business entities and the candidate views.
-- ops:     pipeline bookkeeping (migrations, data versions, view refresh state).
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ops;

-- Every business entity carries a non-null ISO 3166-1 alpha-2 country code.
-- Columns using this domain are additionally declared NOT NULL.
CREATE DOMAIN core.country_code AS text
    CHECK (VALUE ~ '^[A-Z]{2}$');
