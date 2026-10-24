# Complete MapPLUTO throughput run, 2026-10-24

The existing native DuckDB extension processed **all 856,687 MapPLUTO rows in
16 minutes 47 seconds**, without an out-of-memory failure. End-to-end elapsed time
was **16 minutes 56 seconds**, excluding file hashing and connection creation.
Peak process RSS was **923.94 MiB**, with one DuckDB thread, a `1GB` DuckDB limit,
and 1,000-input batches. This is one measured pass, not an estimate extrapolated
from a sample and not a benchmark of the Python matcher.

| Measurement | Result |
|---|---:|
| Input rows processed | 856,687 |
| Nonblank reference addresses | 856,160 |
| Distinct indexed phrases | 605,359 |
| Input batches | 857 |
| Read and prepare source | 1.03 s |
| Prepare normalized reference | 2.56 s |
| Build phrase index | 4.36 s |
| Full matching wall time | 1,006.64 s |
| Matching SQL plus result inserts | 1,005.69 s |
| Validation and Parquet export | 1.65 s |
| Total elapsed, including setup/checkpoints | 1,016.31 s |
| Matching throughput | 851.03 rows/s |
| Peak whole-process RSS | 923.94 MiB |

The full reference was available to every batch. No embeddings, exact-match shortcut,
borough partitions, or input deduplication were used. Default matching parameters
were retained: top one, minimum similarity zero, phrase frequency strictly below
1,000. Inputs were processed once in BBL order, without warmups; filesystem caches
were uncontrolled. Matching wall time includes input-table preparation, writing
results to DuckDB, and periodic progress logging. Validation/export are separate.
Peak RSS covers the entire process across setup and matching, including Python;
it is not a per-batch allocation delta or a guarantee for arbitrary workloads.

## Source and interpretation

This uses the existing local `mappluto.parquet` from the Moses **26v2m2** build:
856,687 unique BBLs. The official PLUTO **26v2** reference CSV was already downloaded
from NYC DCP's publishing endpoint. Joining on BBL verified that every MapPLUTO row
is present there and that all street addresses, boroughs, and ZIP codes agree.
Three latitude values differ by 0.0000001 degree at the local file's precision;
the benchmark retains the local MapPLUTO coordinates. This is a locally rebuilt
MapPLUTO artifact, not a newly downloaded official shapefile.

Official comparison source:
`https://edm-publishing.nyc3.digitaloceanspaces.com/db-pluto/publish/26v2/pluto/pluto.zip`

Official unzipped PLUTO CSV SHA-256:
`c65fac81cdc119ac4528fc78d11474419cd50d0fde1a1b06be42ba8961d24cf9`.
The benchmark summary records the exact MapPLUTO Parquet SHA-256.

Each input combines street address, expanded borough name, `NY`, and ZIP. The same
source supplies the reference addresses and coordinates, making this a **self-retrieval
test of throughput and matching behavior**, not independently validated geocoding.
Blank street addresses remain NULL inputs and do not enter the reference. Geometry
and unrelated MapPLUTO attributes are not loaded into the matching tables.

## Retrieval results

| Outcome | Rows |
|---|---:|
| Returned a match | 852,545 |
| Returned the input BBL | 830,363 |
| Returned the same normalized address | 847,630 |
| Same normalized address, different BBL | 17,267 |
| Returned a different normalized address | 4,915 |
| Unmatched | 4,142 |

The 4,142 unmatched inputs are fully accounted for:

- 527 blank street addresses.
- 358 nonblank inputs with no numeric tokens, which fail the numeric gate.
- 3,257 inputs whose phrases are all excluded by the frequency cutoff.

The reference contains 5,464 groups of duplicate normalized addresses. Selecting
a different BBL with the same address can reflect those ambiguities. Different
normalized addresses require separate review: **all 852,545 returned matches have
similarity 1.0**, including all 4,915 different-address selections. A perfect score
is therefore insufficient to establish an exact address match.

For example, `15 SOUTH WILLIAM STREET, MANHATTAN, NY 10004` selects
`15 WILLIAM STREET, MANHATTAN, NY 10004` at score 1.0. The alpha-token overlap is
divided by the smaller token-set size, so a subset can receive a perfect score;
reference ID then breaks a tie. `343 WEST BROADWAY` similarly selects `343 BROADWAY`.
Before production geocoding, prioritize exact normalized matches and review how
directional/street tokens affect ranking. Increasing the phrase cutoff would also
need a new memory/throughput measurement. Neither behavior was changed for this run.

## Reproduce and inspect

Environment: macOS 26.5.2 ARM64, Python 3.12.15, DuckDB 1.4.2; native extension
SHA-256 `643986f88afdb6ebe30968db44ee8328a78334c665db446a5177f293853cde37`.
Base Git revision is `f1d41a8`; the runner was newly added and uncommitted at run
start, so `git_dirty` is true. Its exact source hash is recorded, and the runner
is committed alongside this report. No extension implementation changes were made.

```sh
uv run python extensions/duckdb/benchmarks/mappluto.py \
  --source /path/to/mappluto.parquet \
  --extension extensions/duckdb/build/whereabouts.duckdb_extension \
  --out extensions/duckdb/build/mappluto-26v2m2-1gb \
  --batch-size 1000 --threads 1 --memory-limit 1GB
```

- [Full summary and all batch timings](mappluto-26v2m2-macos-2026-10-24.json).
- [Unmatched reasons, borough counts, and discrepancy examples](mappluto-26v2m2-macos-2026-10-24-diagnostics.json).
- [Harness methodology](../README.md#complete-mappluto-throughput-run).

Local ignored artifacts under the output directory retain `results.parquet`
(one row per input), `different_bbl.parquet`, and `run.duckdb` with the full source,
reference, index, and results. Supplementary diagnostics were run after timing.
The complete source parquet and large generated artifacts are not committed.

Validation: all 856,687 output IDs are unique and output cardinality matches input;
no blank input returned a match. Full test suite: **75 passed, 16 skipped**.
The added harness regression test covers final partial batches, blank inputs,
duplicate address labels with distinct BBLs, and exported row counts. Ruff passes.
