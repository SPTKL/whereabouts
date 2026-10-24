# Standard matcher benchmarks

This harness compares the existing Python implementation with the DuckDB extension
on the same machine, DuckDB version, thread count, reference source, and input batch.
It measures accuracy separately from performance. No geocoder implementation is
changed by this benchmark branch.

The first [macOS ARM64 baseline](results/macos-arm64-2026-10-23.md) records measured
speed, memory failures, accuracy, and diagnostics explaining compatibility differences.
The [complete MapPLUTO run](results/mappluto-26v2m2-macos-2026-10-24.md) measures
all 856,687 rows against the full citywide reference with bounded input batches.

## Run

From the repository root:

```sh
uv sync --locked
# One-time prerequisite for the original Python matcher; not timed or downloaded by the benchmark.
uv run python -c "import duckdb; duckdb.connect().execute('INSTALL splink_udfs FROM community')"
uv run make -C extensions/duckdb fetch
uv run make -C extensions/duckdb

uv run python extensions/duckdb/benchmarks/compare.py \
  --extension extensions/duckdb/build/whereabouts.duckdb_extension \
  --dataset all --samples 128 --sizes 100 1000 10000 \
  --threads 1 --memory-limit 1GB --repeats 3 --warmups 1 \
  --out extensions/duckdb/build/benchmarks/run-1
```

Use a new output directory for each run; existing results are never overwritten.
Omit `--extension` to test the SQL-only distribution. The binary must match the host
platform; the harness loads it into the package's pinned DuckDB 1.4.2 for both sides
of the comparison. The CLI used to build the binary is not the benchmark engine.

Run performance trials on an otherwise idle machine. The worker jobs run sequentially
in a seeded, shuffled order, never concurrently. Repeat full runs before interpreting
small performance differences. Increase `--repeats` for a more stable distribution.
`--timeout` limits each worker (default 300 seconds).
Failed or timed-out workers are retained in the report and other jobs continue.
The overall command exits with status 2 if any worker failed; inspect `summary.json`
and the per-worker logs instead of interpreting failures as zero throughput.

## Three measured execution paths

| Engine | Timed operation |
|---|---|
| `python_query` | Existing standard SQL pipeline + its real Python UDFs, projected to five common output columns and fully fetched |
| `extension_query` | `wa_geocode` through the loaded extension, the same five output columns fully fetched |
| `python_api` | The unmodified `Matcher.geocode` method, including pandas input conversion, table loading, full result payload, sorting, and dictionary conversion |

**Compare `python_query` with `extension_query` for matching-query speed.** Input
tables are preloaded for both. Comparing with `python_api` also includes API and
larger-payload overhead, so that ratio is not an isolated native-vs-Python speedup.
The extension query is hosted in Python for an equivalent DuckDB runtime and result
consumer; these are not standalone CLI process-memory measurements.

Every worker uses a fresh process and connection, then records:

- Setup time separately from query time.
- First call latency (not a cold disk/cache measurement).
- An excluded warmup, followed by all measured repetitions and their median.
- Fully materialized rows per second.
- Process peak RSS and pre-query high-water RSS. This includes Python, pandas,
  connection/reference state, inputs, and results across all trials. It is not
  DuckDB-only memory or an allocation delta.
- Repeated-result equality and exact output cardinality/ID checks.

The first-call result must equal every subsequent result. Python `Matcher` and
`AddressLoader` receive benchmark-controlled connections to select database paths,
thread counts, and memory limits without installing extensions or writing into the
package's models directory. Their matching/build methods are unmodified; constructor
behavior and downloads are outside the comparison. Reference build times include
each implementation's normalization and index creation, but not source-file reading
or extension loading. They are single observations, not repeated throughput trials.

## Data and accuracy

Default references are the repository's 18,687 Australian addresses and 121 Polish
addresses. Each implementation independently builds its reference and phrase index
from the same records. Input/reference IDs are mapped to common deterministic integer
IDs for the original Python schema. The extension still compares their string form
when breaking ties, as its API specifies.

The generated cases use seeded samples of real reference addresses with exact text,
formatting changes, abbreviation expansions, single-letter edits, omitted postcodes,
and removed combining accents. Unchanged variants are omitted. Clearly disjoint
synthetic addresses and blank/NULL inputs provide negative controls. These edits
are **synthetic regression labels, not independently adjudicated real input**; an
edit may introduce genuine ambiguity. They do not establish general production
accuracy. Larger performance batches repeat the fixed case set with fresh input IDs;
they are not additional independent accuracy observations or larger reference corpora.

The 244 published Australian labels are evaluated separately. Only one expected
address is present in the bundled local Australian reference. The other 243 cannot
establish in-coverage matching accuracy. The harness records label coverage rather
than silently discarding those cases or constructing a reference from their answers.

For each engine and case category, results include thresholds 0, 0.25, 0.5, 0.75,
and 0.9. Both engines return their unfiltered top result; the **same postprocessing
threshold** is applied to each. This avoids relying on the Python standard path's
currently unused `threshold` setting. Metrics distinguish:

- Positive recall and recall restricted to labels present in the reference.
- Precision among accepted matches.
- False-positive rate and correct rejections for labeled no-match cases.
- Agreement with Python, score differences, and maximum coordinate displacement.

Label comparison ignores case, spacing, and punctuation only. It does not remove
accents, expand abbreviations, reorder tokens, or use geocoder-specific normalization.
Coordinate displacement compares the two predictions; it is not absolute geolocation
accuracy against independently surveyed coordinates. Reference coordinates are
validated by the extension, and Python's reference builder stores them as FLOAT.

An additional `extension_query/shared` control uses the Python reference text,
numeric tokens, and phrase index unchanged. It helps distinguish reference preparation
differences from query/scoring behavior. It does not remove the extension's input
normalization and tie-order differences. Full label/score disagreements are written
to CSV, including category, expected address, both predictions, and both scores.

## Bring representative data

```sh
uv run python extensions/duckdb/benchmarks/compare.py \
  --dataset custom --reference /path/reference.parquet \
  --cases /path/held-out.csv --samples 128 --sizes 100 1000 10000 \
  --extension extensions/duckdb/build/whereabouts.duckdb_extension \
  --out extensions/duckdb/build/benchmarks/custom-1
```

Reference CSV/Parquet columns: `address_id`, `address`, `latitude`, `longitude`;
optional `suburb`, `postcode`, `state`. This benchmark requires non-NULL coordinates
and unique IDs. Reference addresses need not be unique. Case CSV columns:
`input_address`, `expected_address` (or `best_match`); a blank expected address means
an explicitly labeled no-match. Only use independently labeled no-match examples.
The harness never adds expected labels to the reference database.

Outputs stay under the chosen local directory. `summary.json` contains raw timings,
metrics, preparation statistics, environment settings, Git revision/dirty state,
and SHA-256 hashes of code, binary, inputs, and cases. Per-worker JSON files retain
predictions; disagreement CSVs allow review. Generated databases and large outputs
belong under ignored `build/`, not in Git.

The optional `DuckDB benchmarks (manual)` GitHub Actions workflow runs the comparison
on Linux and macOS and uploads summaries, logs, and disagreement CSVs even if a
worker fails. Hosted-runner timings are useful for reproducibility, not precise
cross-machine performance rankings. The workflow must be available on the fork's
default branch before GitHub exposes its manual dispatch button.

## Complete MapPLUTO throughput run

`mappluto.py` runs every row of a local MapPLUTO Parquet through the standard
extension, using the nonblank addresses in that same file as its reference:

```sh
uv run python extensions/duckdb/benchmarks/mappluto.py \
  --source /path/to/mappluto.parquet \
  --extension extensions/duckdb/build/whereabouts.duckdb_extension \
  --batch-size 1000 --threads 1 --memory-limit 1GB \
  --out extensions/duckdb/build/mappluto/run-1
```

Required source columns: `BBL`, `Address`, `Borough`, `ZipCode`, `Latitude`,
`Longitude`, `Version`. BBLs must be unique and non-NULL. Coordinates must satisfy
the extension's WGS84 validation. The input string combines the street address,
expanded borough name, `NY`, and ZIP. Blank street addresses become NULL inputs,
remain in the output, and are excluded from the reference. All other addresses
are passed through the matcher, without an exact-match shortcut, deduplication,
borough partitioning, or embeddings. Default matcher thresholds are unchanged.

This is a **self-retrieval throughput test**, not independent geocoding accuracy.
Coordinates are retrieved from the reference. Duplicate addresses and tied scores
can legitimately select another BBL; the summary reports BBL and normalized-address
agreement separately. The complete reference remains available to every batch.

The run records source and code hashes, source versions, reference/index build
time, per-batch input preparation and query-plus-insert time, total matching wall
time, and whole-process peak RSS. It is one sequential pass, with no warmup or
repetitions; OS caches are uncontrolled. Matching wall time includes input table
creation and periodic progress writes. Result export and validation are timed
separately. Query timings include inserting results into a disk-backed DuckDB
table and are not identical to `compare.py`'s `fetchall()` timing boundary.

`results.parquet` retains one output per input BBL, `different_bbl.parquet` retains
all different-BBL/unmatched cases, and `run.duckdb` retains the prepared reference,
index, source, and results. These large outputs remain ignored under `build/`.
The output directory must be new; partial runs are recorded but not resumable.

## Batch sizes and spill space

After a complete `mappluto.py` run, reuse its `run.duckdb` to compare larger batches:

```sh
uv run python extensions/duckdb/benchmarks/spill.py \
  --database extensions/duckdb/build/mappluto-26v2m2-1gb/run.duckdb \
  --extension extensions/duckdb/build/whereabouts.duckdb_extension \
  --out extensions/duckdb/build/mappluto-spill/run-1 \
  --sizes 10000 50000 1000 --memory-limit 1GB --max-spill 20GB --threads 1
```

Each batch size runs in a fresh process, sequentially in the supplied order, with
the same complete input and reference. Reference construction is excluded. Each
successful pass is compared field-for-field with the prior complete output,
including NULLs, coordinates, scores, and tie choices. Failed runs retain their
completed-row count, active batch, error, and telemetry; the controller continues
the remaining sizes and exits 2 if any fails. Outputs stay in new, ignored build
directories; partial runs are not resumed.

The DuckDB JSON profiler records `system_peak_buffer_memory` and
`system_peak_temp_dir_size` after each successful matching query. These are process
connection high-water measurements, not additive per-batch sizes. A background
thread additionally samples logical spill-file sizes every 250 ms, including
failed queries for which no complete profile may be produced. Sampled peaks are
lower bounds and are not cumulative bytes written or physical allocated blocks.
The report preserves profiler and filesystem peaks separately. Whole-process peak
RSS is also recorded, with matching-stage RSS captured before result validation.

Every run uses an explicit dedicated `temp_directory` and `max_temp_directory_size`.
Spilling is already supported by default; this experiment measures usage and the
effect of batch size rather than claiming to enable a previously disabled feature.
The RAM setting covers DuckDB-managed memory, not all Python/native allocations.
One pass per size, no warmup, uncontrolled OS caches, fixed order, profiling and
250 ms monitoring overhead mean results are exploratory rather than statistical
performance guarantees. Matching wall time includes input-table creation,
query/result insertion, profile reads, and progress logging. Validation/checkpoint
time is recorded separately. The earlier uninstrumented run is contextual, not a
strictly identical timing comparison.

## Tune the built-in batch size

`batching.py` calls `wa_geocode_batched` once per run; the extension performs all
batches inside SQL. Each size runs in a fresh process. By default it selects 20,000
inputs spread deterministically over the complete BBL-ordered MapPLUTO corpus,
while keeping the **full reference and phrase index**. It compares all result
columns, including unmatched rows, against the completed external-loop baseline.

```sh
uv run python extensions/duckdb/benchmarks/batching.py \
  --database extensions/duckdb/build/mappluto-26v2m2-1gb/run.duckdb \
  --extension extensions/duckdb/build/whereabouts.duckdb_extension \
  --out extensions/duckdb/build/batch-tuning-1gb \
  --sizes 250 500 1000 2000 5000 --memory-limit 1GB --threads 1
```

Use `--sample-rows 0 --sizes 1000` and a fresh output directory to validate a
complete pass. The timed SQL statement includes the input snapshot, global ID
validation, recursive matching, and result-table creation. Source sampling,
reference/index build, result comparison, and checkpoint are outside that timing.
This differs from `spill.py`, which times an external Python batching loop.

Batch size controls inputs per matching iteration, not a byte budget. Tune on the
actual corpus and input distribution; changing phrase thresholds, thread count, or
memory limit can change the best choice. A single sample pass identifies promising
sizes, not a universal or statistically established optimum. Validate the selected
size on the complete workload and retain memory headroom.

Measured results:

- [Built-in SQL batch-size sweep](results/mappluto-batching-macos-2026-10-24.md)
- [Memory-limit and spill experiment](results/mappluto-spill-macos-2026-10-24.md)

## Remaining evaluation

Before upstream release: use a much larger reference corpus with representative,
independent held-out inputs and verified no-matches; review disagreements rather than
treating Python as ground truth; benchmark several thread counts, batch sizes, and
platforms; measure country-scale index/storage costs; and set explicit accuracy and
performance regression tolerances. Trigram/reverse-geocoding comparisons require
implementing those modes in the extension first.
