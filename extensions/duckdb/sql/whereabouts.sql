-- Adapted from Alex Lee's Whereabouts standard matcher (MIT).
-- See licenses/whereabouts-MIT.txt and THIRD_PARTY_NOTICES.md.
-- Installed macros live in the current database; reference data is ordinary SQL tables.
CREATE OR REPLACE MACRO wa_normalize(address) AS
    trim(regexp_replace(strip_accents(upper(CAST(address AS VARCHAR))), '[^\p{L}\p{N}]+', ' ', 'g'));

CREATE OR REPLACE MACRO _wa_numerics(address) AS
    regexp_extract_all(address, '[0-9]+\p{L}?');
CREATE OR REPLACE MACRO _wa_alpha(address) AS
    regexp_extract_all(address, '\p{L}+');
CREATE OR REPLACE MACRO _wa_iou(a, b) AS
    CASE WHEN a IS NULL OR b IS NULL THEN NULL
         WHEN len(a) = 0 THEN 0.0
         ELSE len(list_intersect(a, b))::DOUBLE / list_unique(list_concat(a, b)) END;
CREATE OR REPLACE MACRO _wa_overlap_min(a, b) AS
    CASE WHEN a IS NULL OR b IS NULL THEN NULL
         WHEN least(list_unique(a), list_unique(b)) = 0 THEN 0.0
         ELSE len(list_intersect(a, b))::DOUBLE / least(list_unique(a), list_unique(b)) END;
CREATE OR REPLACE MACRO _wa_numeric_gate(a, b) AS
    CASE WHEN a IS NULL OR b IS NULL OR len(a) = 0 THEN false
         ELSE len(list_intersect(a, b))::DOUBLE / len(a) >= 0.2 END;

-- Source columns: address_id, address, latitude, longitude.
-- Preserve postal codes, localities, and other caller metadata in the source;
-- reference_id is the key for joining them back to matches.
CREATE OR REPLACE MACRO wa_prepare_reference(source_table) AS TABLE
WITH _wa_source AS MATERIALIZED (
    SELECT CAST(address_id AS VARCHAR) AS reference_id,
           CAST(address AS VARCHAR) AS address,
           CAST(latitude AS DOUBLE) AS latitude,
           CAST(longitude AS DOUBLE) AS longitude
    FROM query_table(source_table)
), _wa_checked AS (
    SELECT *,
        CASE WHEN reference_id IS NULL OR reference_id = '' THEN error('Reference address_id must be nonempty and non-NULL')
             WHEN count(*) OVER (PARTITION BY reference_id) > 1 THEN error('Reference address_id must be unique after conversion to VARCHAR')
             WHEN address IS NULL OR wa_normalize(address) = '' THEN error('Reference address must be nonempty and non-NULL')
             WHEN (latitude IS NULL) != (longitude IS NULL) THEN error('Reference coordinates must both be present or both be NULL')
             WHEN latitude IS NOT NULL AND (NOT isfinite(latitude) OR NOT isfinite(longitude) OR abs(latitude) > 90 OR abs(longitude) > 180)
                 THEN error('Reference coordinates must be finite WGS84 latitude/longitude')
             ELSE true END AS valid
    FROM _wa_source
)
SELECT reference_id, address, wa_normalize(address) AS normalized_address,
       _wa_numerics(normalized_address) AS numeric_tokens,
       _wa_alpha(normalized_address) AS alpha_tokens,
       latitude, longitude
FROM _wa_checked WHERE valid;

CREATE OR REPLACE MACRO _wa_phrase_parts(parts) AS
    list_distinct(list_concat(
        [parts[_wa_pos] || ' ' || parts[_wa_pos+1]
         FOR _wa_pos IN range(1, len(parts))],
        [parts[_wa_pos] || ' ' || parts[_wa_pos+2]
         FOR _wa_pos IN range(1, len(parts)-1)
         IF TRY_CAST(parts[_wa_pos] AS INTEGER) IS NOT NULL]
    ));
CREATE OR REPLACE MACRO _wa_phrases(address) AS
    _wa_phrase_parts(string_split(address, ' '));

-- Materialize once per reference release. Rebuild whenever reference rows change.
CREATE OR REPLACE MACRO wa_build_index(reference_table) AS TABLE
WITH _wa_phrases AS MATERIALIZED (
    SELECT reference_id AS record_id, unnest(_wa_phrases(normalized_address)) AS phrase
    FROM query_table(reference_table)
)
SELECT phrase, list(record_id ORDER BY record_id) AS reference_ids,
       count(*) AS frequency
FROM _wa_phrases GROUP BY phrase;

CREATE OR REPLACE MACRO wa_geocode(
    input_table, reference_table, index_table,
    top_n := 1, min_similarity := 0.0, max_phrase_frequency := 1000
) AS TABLE
WITH _wa_parameters AS MATERIALIZED (
    SELECT
        CASE WHEN top_n IS NULL OR top_n < 1 OR top_n > 1000 OR top_n != trunc(top_n)
             THEN error('top_n must be an integer between 1 and 1000') ELSE CAST(top_n AS BIGINT) END AS n,
        CASE WHEN min_similarity IS NULL OR NOT isfinite(min_similarity) OR min_similarity < 0 OR min_similarity > 1
             THEN error('min_similarity must be between 0 and 1') ELSE CAST(min_similarity AS DOUBLE) END AS cutoff,
        CASE WHEN max_phrase_frequency IS NULL OR max_phrase_frequency < 1 OR NOT isfinite(max_phrase_frequency) OR max_phrase_frequency != trunc(max_phrase_frequency)
             THEN error('max_phrase_frequency must be a positive integer') ELSE CAST(max_phrase_frequency AS BIGINT) END AS max_frequency
), _wa_source AS MATERIALIZED (
    SELECT CAST(address_id AS VARCHAR) AS address_id, CAST(address AS VARCHAR) AS input_address
    FROM query_table(input_table)
), _wa_inputs AS MATERIALIZED (
    SELECT address_id, input_address,
           wa_normalize(input_address) AS normalized_address,
           _wa_numerics(normalized_address) AS numeric_tokens,
           _wa_alpha(normalized_address) AS alpha_tokens
    FROM (
        SELECT *, CASE WHEN address_id IS NULL OR address_id = '' THEN error('Input address_id must be nonempty and non-NULL')
                       WHEN count(*) OVER (PARTITION BY address_id) > 1 THEN error('Input address_id must be unique after conversion to VARCHAR')
                       ELSE true END AS valid
        FROM _wa_source
    ) WHERE valid
), _wa_input_phrases AS MATERIALIZED (
    SELECT address_id, unnest(_wa_phrases(normalized_address)) AS phrase
    FROM _wa_inputs
), _wa_candidates AS MATERIALIZED (
    SELECT DISTINCT p.address_id, unnest(idx.reference_ids) AS reference_id
    FROM _wa_input_phrases p
    JOIN query_table(index_table) idx USING (phrase)
    CROSS JOIN _wa_parameters cfg
    WHERE idx.frequency < cfg.max_frequency
), _wa_scores AS MATERIALIZED (
    SELECT c.address_id, r.reference_id, r.address AS matched_address, r.latitude, r.longitude,
           _wa_overlap_min(i.alpha_tokens, r.alpha_tokens) * _wa_iou(i.numeric_tokens, r.numeric_tokens) AS similarity
    FROM _wa_candidates c
    JOIN _wa_inputs i USING (address_id)
    JOIN query_table(reference_table) r USING (reference_id)
    WHERE _wa_numeric_gate(i.numeric_tokens, r.numeric_tokens)
), _wa_ranked AS (
    SELECT s.*, row_number() OVER (PARTITION BY address_id ORDER BY similarity DESC, reference_id) AS match_rank
    FROM _wa_scores s CROSS JOIN _wa_parameters cfg
    WHERE similarity >= cfg.cutoff
), _wa_selected AS (
    SELECT r.* FROM _wa_ranked r CROSS JOIN _wa_parameters cfg WHERE match_rank <= cfg.n
)
SELECT i.address_id, i.input_address, s.reference_id, s.matched_address,
       s.latitude, s.longitude, s.similarity, s.match_rank
FROM _wa_inputs i LEFT JOIN _wa_selected s USING (address_id);

CREATE OR REPLACE MACRO wa_version() AS '0.1.0';
