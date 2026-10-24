# MapPLUTO: batch size, memory limit, and disk spill

Measured on macOS ARM64, DuckDB 1.4.2, one thread, with all 856,687 MapPLUTO
26v2m2 inputs and the same prepared 856,160-row reference and 605,359-phrase index.
These runs use `spill.py`'s **external Python loop**, before built-in SQL batching.
See [machine-readable summaries](mappluto-spill-macos-2026-10-24.json) for source
hashes, exact timings, runtime versions, and errors. The full per-batch profiles
and databases are ignored local build artifacts.

| Memory limit | Inputs/batch | Outcome | Matching time | Peak process RSS | Sampled peak spill |
|---|---:|---|---:|---:|---:|
| 1 GB | 1,000 | All 856,687 rows; zero result differences | 17:25.9 | 638 MiB | 0 |
| 1 GB | 10,000 | OOM on second batch; 10,000 completed | 30.3 s before error | 1,359 MiB | ≥0.76 GiB |
| 1 GB | 50,000 | OOM on first batch | 82.9 s before error | 1,387 MiB | ≥5.62 GiB |
| 4 GB | 10,000 | All 856,687 rows; zero result differences | 16:47.1 | 2,801 MiB | 0 |
| 4 GB | 50,000 | OOM on first batch | 88.8 s before error | 3,134 MiB | ≥3.10 GiB |

The 4 GB / 10,000 configuration completed about 3.7% sooner than 1 GB / 1,000,
with roughly 4.4 times the peak process RSS. This modest difference comes from
single runs; it does not establish a statistically significant speed advantage.
The original uninstrumented 1,000-row run took 16:46.6, illustrating why small
cross-run timing differences should not be overinterpreted.

Disk spill was enabled with a dedicated directory and a 20 GB temporary-space cap
in every run. The failed large batches exhausted managed memory, not the disk cap.
Spilling therefore does not make arbitrarily large matching batches safe. DuckDB's
managed memory limit also differs from total process RSS.

Successful-query profiler peaks for 1 GB / 1,000 and 4 GB / 10,000 were 438.9 and
2,759.5 MiB of buffer memory, respectively, with no spill. The first successful
10,000-row batch at 1 GB profiled 953.7 MiB of buffer memory and 325.1 MiB of spill;
the later failed batch raised sampled spill to at least 775.5 MiB. Runs that fail
before their first successful query have zero recorded profiler fields because
**no completed profile exists**; those zeros are not memory-usage measurements.
Spill sampling runs every 250 ms and records logical file sizes, so nonzero peaks
are lower bounds, not cumulative disk traffic or physical allocated bytes.

One fresh worker process ran each size, sequentially, with no warmup or OS-cache
control. Order was 10,000 → 50,000 → 1,000 at 1 GB, then 10,000 → 50,000 at 4 GB.
Reference/index construction is excluded. Matching time includes per-batch input
creation, matching/insertion, profile reads, and progress logging. Result validation
and checkpoint are excluded. Light code editing and a small SQL prototype occurred
while the 4 GB runs were active; build and test suites ran afterward.

All completed runs were compared on every output column against the original full
pass and had zero differences. This is a self-retrieval consistency test, not an
independent measurement of geocoding accuracy; the original ranking and retrieval
limitations remain unchanged.
