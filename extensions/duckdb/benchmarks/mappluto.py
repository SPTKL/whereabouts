"""Time a complete MapPLUTO self-retrieval pass through the standard matcher."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import resource
import subprocess
import time

import duckdb


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def peak_rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024**2 if platform.system() == "Darwin" else 1024)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--memory-limit", default="1GB")
    args = parser.parse_args()
    if args.batch_size < 1 or args.threads < 1:
        parser.error("batch size and thread count must be positive")
    args.out.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[3]
    summary = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(args.source.resolve()),
        "source_sha256": sha256(args.source),
        "runner_sha256": sha256(Path(__file__)),
        "extension_sha256": sha256(args.extension),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True
            ).strip()
        ),
        "duckdb": duckdb.__version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "threads": args.threads,
        "memory_limit": args.memory_limit,
        "batch_size": args.batch_size,
        "kind": "self-retrieval; inputs and reference use the same source",
        "stages_seconds": {},
        "batches": [],
    }
    stages = summary["stages_seconds"]
    summary_path = args.out / "summary.json"

    def save() -> None:
        summary["peak_process_rss_mib"] = peak_rss_mib()
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    con = duckdb.connect(
        str(args.out / "run.duckdb"),
        config={
            "threads": args.threads,
            "memory_limit": args.memory_limit,
            "allow_unsigned_extensions": "true",
        },
    )
    start = time.perf_counter()
    stage = "load_extension"
    try:
        binary = str(args.extension.resolve()).replace("'", "''")
        con.execute(f"LOAD '{binary}'")
        stages[stage] = time.perf_counter() - start
        stage = "read_prepare_source"
        tick = time.perf_counter()
        con.read_parquet(str(args.source)).create_view("source_file")
        con.execute("""CREATE TABLE source AS
            SELECT row_number() OVER (ORDER BY BBL) AS seq,
                   CAST(CAST(BBL AS BIGINT) AS VARCHAR) AS address_id,
                   Address AS street_address, Borough AS borough,
                   CASE WHEN Address IS NULL OR trim(Address) = '' THEN NULL
                     ELSE concat_ws(', ', trim(Address),
                         CASE Borough WHEN 'MN' THEN 'MANHATTAN' WHEN 'BX' THEN 'BRONX'
                           WHEN 'BK' THEN 'BROOKLYN' WHEN 'QN' THEN 'QUEENS'
                           WHEN 'SI' THEN 'STATEN ISLAND' ELSE Borough END,
                         concat_ws(' ', 'NY', CAST(CAST(ZipCode AS BIGINT) AS VARCHAR)))
                   END AS address,
                   CAST(Latitude AS DOUBLE) AS latitude,
                   CAST(Longitude AS DOUBLE) AS longitude, Version AS version
            FROM source_file""")
        counts = con.execute("""SELECT count(*), count(DISTINCT address_id),
            count(*) FILTER (WHERE address_id IS NULL),
            count(*) FILTER (WHERE address IS NULL), min(version),max(version)
            FROM source""").fetchone()
        total, unique_ids, null_ids, blank_addresses, min_version, max_version = counts
        if unique_ids != total or null_ids:
            raise ValueError("Source BBLs must be unique and non-NULL")
        summary.update(
            input_rows=total,
            blank_address_rows=blank_addresses,
            source_versions=[min_version, max_version],
        )
        con.execute(
            "CREATE VIEW reference_source AS SELECT * FROM source WHERE address IS NOT NULL"
        )
        stages[stage] = time.perf_counter() - tick
        print(
            f"Source: {total:,} rows; {blank_addresses:,} blank addresses", flush=True
        )
        stage = "prepare_reference"
        tick = time.perf_counter()
        con.execute(
            "CREATE TABLE wa_reference AS FROM wa_prepare_reference('reference_source')"
        )
        stages[stage] = time.perf_counter() - tick
        stage = "build_index"
        tick = time.perf_counter()
        con.execute("CREATE TABLE wa_index AS FROM wa_build_index('wa_reference')")
        stages[stage] = time.perf_counter() - tick
        summary["reference_rows"] = con.execute(
            "SELECT count(*) FROM wa_reference"
        ).fetchone()[0]
        summary["index_phrases"] = con.execute(
            "SELECT count(*) FROM wa_index"
        ).fetchone()[0]
        summary["duplicate_normalized_address_groups"] = (
            con.execute("""SELECT count(*) FROM (
            SELECT normalized_address FROM wa_reference GROUP BY ALL HAVING count(*)>1)""").fetchone()[
                0
            ]
        )
        con.execute("""CREATE TABLE results (
            address_id VARCHAR, input_address VARCHAR, reference_id VARCHAR,
            matched_address VARCHAR, latitude DOUBLE, longitude DOUBLE,
            similarity DOUBLE, match_rank BIGINT)""")
        con.execute("CHECKPOINT")
        summary["database_bytes_before_matching"] = (
            (args.out / "run.duckdb").stat().st_size
        )
        save()
        print(f"Prepared reference and index: {stages}", flush=True)
        stage = "matching"
        matching_start = time.perf_counter()
        last_progress = matching_start
        for first in range(1, total + 1, args.batch_size):
            last = min(first + args.batch_size - 1, total)
            tick = time.perf_counter()
            con.execute(
                """CREATE OR REPLACE TEMP TABLE input_addresses AS
                SELECT address_id,address FROM source WHERE seq BETWEEN ? AND ?""",
                [first, last],
            )
            input_seconds = time.perf_counter() - tick
            tick = time.perf_counter()
            con.execute("""INSERT INTO results
                SELECT * FROM wa_geocode('input_addresses','wa_reference','wa_index')""")
            query_seconds = time.perf_counter() - tick
            summary["batches"].append(
                {
                    "first": first,
                    "last": last,
                    "rows": last - first + 1,
                    "input_seconds": input_seconds,
                    "query_and_insert_seconds": query_seconds,
                }
            )
            elapsed = time.perf_counter() - matching_start
            if time.perf_counter() - last_progress >= 30 or first == 1 or last == total:
                print(
                    f"Matched {last:,}/{total:,}: {elapsed:.1f}s, {last / elapsed:.1f} rows/s, "
                    f"RSS peak {peak_rss_mib():.0f} MiB",
                    flush=True,
                )
                save()
                last_progress = time.perf_counter()
        stages[stage] = time.perf_counter() - matching_start
        summary["matching_query_and_insert_seconds"] = sum(
            batch["query_and_insert_seconds"] for batch in summary["batches"]
        )
        summary["matching_rows_per_second"] = total / stages[stage]
        stage = "validate_and_export"
        tick = time.perf_counter()
        result_rows, result_ids = con.execute(
            "SELECT count(*),count(DISTINCT address_id) FROM results"
        ).fetchone()
        if (result_rows, result_ids) != (total, total):
            raise AssertionError("Output must contain exactly one row per input BBL")
        metrics = con.execute("""SELECT count(*) FILTER (WHERE r.reference_id IS NOT NULL),
            count(*) FILTER (WHERE r.reference_id IS NULL),
            count(*) FILTER (WHERE r.reference_id = s.address_id),
            count(*) FILTER (WHERE wa_normalize(r.matched_address) = wa_normalize(s.address)),
            count(*) FILTER (WHERE r.similarity = 1.0),
            count(*) FILTER (WHERE s.address IS NULL AND r.reference_id IS NOT NULL)
            FROM results r JOIN source s USING (address_id)""").fetchone()
        summary["retrieval"] = dict(
            zip(
                [
                    "matched",
                    "unmatched",
                    "same_bbl",
                    "same_normalized_address",
                    "score_one",
                    "blank_input_matched",
                ],
                metrics,
                strict=True,
            )
        )
        con.execute(
            "COPY results TO ? (FORMAT PARQUET)", [str(args.out / "results.parquet")]
        )
        con.execute(
            """COPY (SELECT s.street_address,s.borough,s.latitude AS source_latitude,
            s.longitude AS source_longitude,r.* FROM source s JOIN results r USING(address_id)
            WHERE r.reference_id IS DISTINCT FROM s.address_id)
            TO ? (FORMAT PARQUET)""",
            [str(args.out / "different_bbl.parquet")],
        )
        con.execute("CHECKPOINT")
        stages[stage] = time.perf_counter() - tick
        summary["status"] = "ok"
        summary["total_seconds_excluding_hashes_and_connection"] = (
            time.perf_counter() - start
        )
        save()
        print(
            json.dumps({k: v for k, v in summary.items() if k != "batches"}, indent=2),
            flush=True,
        )
    except Exception as exc:
        summary.update(
            status="error",
            failed_stage=stage,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        summary["total_seconds_excluding_hashes_and_connection"] = (
            time.perf_counter() - start
        )
        save()
        raise
    finally:
        con.close()


if __name__ == "__main__":
    main()
