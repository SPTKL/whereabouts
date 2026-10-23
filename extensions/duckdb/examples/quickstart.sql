-- First LOAD the absolute path to whereabouts.duckdb_extension, or .read sql/whereabouts.sql.
-- These coordinates are illustrative test data, not authoritative geocodes.
CREATE TABLE address_source AS SELECT * FROM (VALUES
    ('nyc-1', '125 Worth Street New York NY 10013', 40.715, -74.002),
    ('paris-1', '10 Rue de l’École Paris 75005', 48.849, 2.345),
    ('sydney-1', '1 George Street Sydney NSW 2000', -33.86, 151.21)
) t(address_id, address, latitude, longitude);

CREATE TABLE address_reference AS
SELECT * FROM wa_prepare_reference('address_source');
CREATE TABLE address_index AS
SELECT * FROM wa_build_index('address_reference');

CREATE TABLE incoming AS SELECT * FROM (VALUES
    ('request-1', '125 Worth St New York NY 10013'),
    ('request-2', '10 rue de l école Paris 75005'),
    ('request-3', '999 Unknown Road')
) t(address_id, address);

SELECT * FROM wa_geocode('incoming', 'address_reference', 'address_index',
                         top_n := 3, min_similarity := 0.5)
ORDER BY address_id, match_rank;
