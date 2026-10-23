# Standard matcher benchmarks

This harness compares the existing Python implementation with the DuckDB extension
on the same machine, DuckDB version, thread count, reference source, and input batch.
It measures accuracy separately from performance. No geocoder implementation is
changed by this benchmark branch.

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

## Remaining evaluation

Before upstream release: use a much larger reference corpus with representative,
independent held-out inputs and verified no-matches; review disagreements rather than
treating Python as ground truth; benchmark several thread counts, batch sizes, and
platforms; measure country-scale index/storage costs; and set explicit accuracy and
performance regression tolerances. Trigram/reverse-geocoding comparisons require
implementing those modes in the extension first.
