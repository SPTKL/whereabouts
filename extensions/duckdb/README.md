# Whereabouts for DuckDB (experimental)

Batch forward geocoding in SQL using the Whereabouts standard matching algorithm.
The extension packages SQL macros in a small native binary. Matching runs entirely
inside DuckDB: no Python interpreter, external service, Geosupport installation,
or DuckDB source build is required at runtime.

This is an initial contribution, with a distinct SQL API. It supports user-supplied
reference data and the **standard** matcher. Trigram matching, reverse geocoding,
pre-built model downloads, and automatic street abbreviation expansion are not
implemented. It is not published in the DuckDB community extension repository.

## Build and load

From this directory, with a C++17 compiler, Make, curl, and unzip installed:

```sh
make fetch                     # Checksum-pinned DuckDB CLI
make -j2                       # No Python needed to compile
build/deps/1.5.6/duckdb -unsigned
```

In DuckDB, use the absolute path to the resulting binary:

```sql
LOAD '/absolute/path/whereabouts/extensions/duckdb/build/whereabouts.duckdb_extension';
SELECT wa_version();
.read examples/quickstart.sql
```

The build defaults to DuckDB 1.5.6. The native test matrix covers macOS ARM64 and
Linux x86-64 on 1.4.4, 1.5.0, and 1.5.6; Linux coverage runs in CI. The SQL parity
tests also use the Python package's pinned DuckDB 1.4.2. Windows and Wasm builds
are not configured. The C API entrypoint targets version 1.2.0, but the SQL API
requires newer DuckDB features; this is not a claim of support for DuckDB 1.2.

To use a different tested host version:

```sh
make fetch DUCKDB_VERSION=1.4.4
make test DUCKDB_VERSION=1.4.4
```

For a binary-free installation, execute `sql/whereabouts.sql` with any DuckDB
client, or `.read sql/whereabouts.sql` in the CLI. Both distributions install the
same macros. SQL-only installation needs no unsigned-extension setting.

**Installation writes SQL macros into the current writable database.** In-memory
connections work normally. In persistent databases these macros survive reopening;
reinstalling replaces names beginning `wa_` and `_wa_`, which are reserved for this
API. Use a writable working database and attach reference databases read-only.
Loading into a fresh read-only database is not supported. The installer does not
download datasets or create reference/index tables automatically.

## Supply a reference dataset

Expose four columns through a table or view:

| Column | Contract |
|---|---|
| `address_id` | Unique, nonempty identifier; converted to VARCHAR |
| `address` | Nonempty full address |
| `latitude` | WGS84 latitude, convertible to DOUBLE |
| `longitude` | WGS84 longitude, convertible to DOUBLE |

Coordinates may both be NULL when using the API for address matching alone.
Present coordinates must be finite and within valid latitude/longitude ranges.
Identifiers must remain unique after conversion to VARCHAR. Normalization must
leave at least one letter or number in every reference address.

Map any dataset's column names in a view. Preserve postal codes and other metadata
as text in the source; include location information in the full address as needed.

```sql
CREATE VIEW source_addresses AS
SELECT id AS address_id, full_address AS address, lat AS latitude, lon AS longitude
FROM read_parquet('addresses.parquet');

CREATE TABLE address_reference AS
SELECT * FROM wa_prepare_reference('source_addresses');

CREATE TABLE address_index AS
SELECT * FROM wa_build_index('address_reference');
```

The reference table contains the original and normalized address, token lists,
coordinates, and `reference_id`. The index stores phrases, lists of reference IDs,
and their frequencies. Treat these as a matched, immutable pair and rebuild both
when the source changes. This first version does not detect stale/mismatched indexes
or support incremental updates. Its schema is not a drop-in replacement for every
existing Whereabouts model database; prepare a new reference/index from source data.

## Geocode a table

Input tables/views require `address_id` and `address`. IDs must be unique, nonempty,
and non-NULL after conversion to VARCHAR. Repeated addresses with different IDs
are valid. NULL and blank addresses are retained as unmatched rows.

```sql
CREATE TABLE incoming AS SELECT * FROM (VALUES
    ('request-1', '125 Worth St New York NY 10013'),
    ('request-2', NULL)
) t(address_id, address);

SELECT *
FROM wa_geocode('incoming', 'address_reference', 'address_index',
                top_n := 3, min_similarity := 0.5)
ORDER BY address_id, match_rank;
```

Results contain `address_id`, original `input_address`, `reference_id`, original
`matched_address`, `latitude`, `longitude`, `similarity`, and `match_rank`.
Every input produces up to `top_n` matches, or one row with NULL match fields.
Similarity is an algorithmic score, not a calibrated probability. Ties break by
the VARCHAR reference ID; output order is unspecified unless you add `ORDER BY`.
Join `reference_id` back to your source to retrieve postal codes, administrative
areas, provenance, or other metadata.

| Option | Default | Meaning |
|---|---|---|
| `top_n` | 1 | Maximum matches per input, integer 1–1000 |
| `min_similarity` | 0.0 | Keep candidates scoring at least this value, in [0,1] |
| `max_phrase_frequency` | 1000 | Only use phrases occurring in fewer than this many reference addresses |

Parameters are constants per query. Tables may be schema-qualified or attached:

```sql
ATTACH 'reference.duckdb' AS ref (READ_ONLY);
SELECT * FROM wa_geocode('incoming', 'ref.address_reference', 'ref.address_index');
```

All table arguments use DuckDB's `query_table`; they are names, not interpolated
SQL statements. Internal CTE names use the reserved `_wa_` prefix. Schema-qualify
source names if they collide with those internal names.

## Relationship to the Python implementation

The port follows the standard pipeline: normalize, extract tokens, generate adjacent
and numeric skip phrases, look up candidates, require numeric overlap, score, and
rank. The score multiplies alphabetic intersection-over-minimum by numeric
intersection-over-union. The numeric gate preserves upstream's duplicate-sensitive
denominator and 0.2 cutoff.

Intentional differences:

- Reference and input text use the same `strip_accents`, uppercase, and Unicode
  letter/number normalization. `splink_udfs.unaccent` equivalence is not assumed.
  Unicode character handling does not establish language-specific address accuracy.
- IDs are VARCHAR, and the original input/matched address strings are returned.
- Phrase frequency counts each reference address once per phrase.
- `top_n` and `min_similarity` are applied. The standard Python path at the initial
  port's baseline selects one result and does not apply those exposed settings.
- All candidates receive the same final score before ranking. The redundant upstream
  top-50 stage is omitted, allowing larger `top_n` values.
- Empty alphabetic token sets score zero rather than dividing by zero.

The numeric gate means inputs without usable ASCII numeric tokens cannot match.
Phrase retrieval also needs a shared phrase; a score alone cannot recover a candidate
that was never retrieved. This is not a global nearest-neighbor search or a complete
international address parser. Corpus-scale accuracy, speed, and index-size benchmarks
remain to be established before recommending production deployment.

## Validation

For reproducible speed, memory, and accuracy comparisons with the Python standard
matcher, see the [benchmark harness](benchmarks/README.md).

```sh
make test                       # 16 CLI tests; Python stdlib only for test driver
make test-sql                   # Same tests against the SQL-only distribution
make package                    # Binary, SQL, examples, docs, and licenses
```

From the repository root, run the normal contributor checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

`tests/test_duckdb_extension.py` checks Python scoring parity, compares the existing
standard pipeline against the SQL port using a shared ASCII reference/index, and
loads the native binary in Python-hosted DuckDB when built. The separate CLI suite
covers top-N, ties, thresholds, NULLs, duplicate IDs, Unicode, invalid references,
read-only attachments, SQL name handling, and a 1,000-row batch. It is skipped by
the root pytest invocation and run explicitly by the native CI workflow.

This contribution leaves the existing Python API unchanged. A later change could
reuse these SQL definitions from Python after broader parity validation and agreement
on the intentional differences above.
