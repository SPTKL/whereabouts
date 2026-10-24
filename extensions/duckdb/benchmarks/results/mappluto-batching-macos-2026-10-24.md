# Built-in SQL batching on MapPLUTO

`wa_geocode_batched` accepts a user-set `batch_size` and executes the standard
matcher in a recursive SQL loop. This report measures the new function itself,
not an external Python batching loop. Default: **1,000 input rows per batch**.

## Complete corpus validation

One `CREATE TABLE ... AS SELECT * FROM wa_geocode_batched(...)` processed all
**856,687 lots in 1,009.80 seconds (16:49.8)** at the default 1,000-row batch size,
with a 1 GB managed memory limit and one thread: **848.4 rows/second**.

- All 856,687 result rows matched the original external-loop baseline on every
  column, including unmatched rows. The symmetric `EXCEPT ALL` comparison found
  zero differences.
- Peak process RSS measured after matching was **789.2 MiB**. DuckDB's profiler
  recorded **591.5 MiB** peak managed buffer memory and **zero spill**.
- The subsequent result-verification/checkpoint step took 1.71 seconds and raised
  overall process peak RSS to **942.7 MiB**. Do not confuse that verification peak
  with the timed geocoding measurement.
- Source setup took 0.31 seconds; the complete worker took 1,011.83 seconds.
  Reference/index building was excluded and the existing prepared corpus reused.

This is comparable to the earlier 1,000-row external-loop run (17:25.9), without
requiring a client-side loop. It uses more memory than that loop because the SQL
function snapshots and validates the whole input. Single-run differences do not
establish a speed improvement. See the
[complete-run metadata](mappluto-batched-full-1gb-macos-2026-10-24.json).

## Batch-size sweep

Twenty thousand inputs spread deterministically across the BBL-ordered MapPLUTO
26v2m2 corpus, with the complete 856,160-reference / 605,359-phrase index. DuckDB
1.4.2, macOS ARM64, one thread, 1 GB managed memory cap, 20 GB spill cap. A new
process executes one SQL statement for each size. All output columns match the
original full-pass baseline exactly, including unmatched inputs.

| Input rows/batch | Matching + result creation | Rows/sec | Matching peak process RSS | Peak managed buffer | Peak spill |
|---:|---:|---:|---:|---:|---:|
| 250 | 33.90 s | 590 | 374 MiB | 193 MiB | 0 |
| 500 | 27.23 s | 734 | 448 MiB | 263 MiB | 0 |
| 1,000 | 24.39 s | 820 | 577 MiB | 373 MiB | 0 |
| 2,000 | 23.49 s | 852 | 852 MiB | 595 MiB | 0 |
| 5,000 | 22.35 s | 895 | 1,258 MiB | 953 MiB | 73 MiB |

The fastest **sampled** size was 5,000, but it used the entire managed memory
budget and spilled. It completed about 8.4% sooner than 1,000 while using over twice
the process RSS. A 2,000-row batch was about 3.7% faster than 1,000 with 48% higher
RSS. These are single passes, with fixed order and no warmup or OS-cache control;
small differences need repeated trials before they can be called reliable gains.

**Use 1,000 as the conservative starting point.** Choose 500 for more headroom;
2,000 is a tuning candidate when more memory is available. Do not assume the fastest
sample setting will fit every full-data batch. This sample preserves geographic
coverage but differs from contiguous full-corpus batches in candidate distribution.
There is no universal optimal batch size: reference ambiguity, phrase thresholds,
input order, thread count, available memory, and downstream SQL all matter.

The [separate external-loop experiment](mappluto-spill-macos-2026-10-24.md) found
10,000-row batches failed at 1 GB and 50,000-row batches failed even at 4 GB, despite
spilling. Raising memory alone does not justify arbitrarily larger batches.

## Reproduction and interpretation

See the [benchmark guide](../README.md#tune-the-built-in-batch-size) and
[raw sample measurements](mappluto-batch-tuning-1gb-macos-2026-10-24.json).
The timed statement includes the input snapshot, global ID validation, recursive
matching, and result-table creation. Reference/index preparation, input sampling,
result comparison, and checkpoint are excluded. The profiler records DuckDB buffer
and spill high-water marks; RSS includes other process allocations and is not the
same quantity as the managed memory limit. Process RSS is read immediately after
matching, before result verification; as a process high-water mark, it also includes
setup. Profiler memory high-water marks can likewise include earlier connection
activity, rather than being an allocation total unique to the matching statement.

These results test retrieval consistency against the previous implementation.
They do not establish real-world geocoding accuracy: MapPLUTO supplies both the
inputs and reference, and the original subset-score / tie-breaking limitations
remain unchanged.
