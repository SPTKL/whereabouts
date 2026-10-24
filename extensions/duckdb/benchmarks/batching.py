"""Measure one SQL call to wa_geocode_batched and verify complete result parity."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

import duckdb

from spill import SpillMonitor, peak_rss_mib, sha256


def worker(args: argparse.Namespace) -> int:
    args.out.mkdir(parents=True, exist_ok=False)
    spill = args.out / "spill"
    spill.mkdir()
    monitor = SpillMonitor(spill)
    summary = {
        "status": "running",
        "batch_size": args.batch_size,
        "memory_limit": args.memory_limit,
        "threads": args.threads,
        "sample_rows_requested": args.sample_rows,
    }

    def save() -> None:
        summary["peak_process_rss_mib"] = peak_rss_mib()
        summary["sampled_peak_spill_bytes"] = monitor.peak_bytes
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    con = duckdb.connect(
        str(args.out / "output.duckdb"),
        config={
            "allow_unsigned_extensions": "true",
            "memory_limit": args.memory_limit,
            "threads": args.threads,
            "temp_directory": str(spill),
            "max_temp_directory_size": "20GB",
        },
    )
    started = time.perf_counter()
    stage = "setup"
    tick = None
    try:
        binary = str(args.extension.resolve()).replace("'", "''")
        database = str(args.database.resolve()).replace("'", "''")
        con.execute(f"LOAD '{binary}'")
        con.execute(f"ATTACH '{database}' AS baseline (READ_ONLY)")
        total = con.execute("SELECT count(*) FROM baseline.source").fetchone()[0]
        # Deterministically spread a sample over the full BBL-ordered corpus.
        # Integer buckets select their first row, preserving geographic coverage.
        if args.sample_rows and args.sample_rows < total:
            con.execute(
                """CREATE TABLE input_addresses AS
                SELECT address_id,address FROM baseline.source
                QUALIFY row_number() OVER (
                    PARTITION BY ((seq-1)::HUGEINT * $sample // $total) ORDER BY seq
                )=1 ORDER BY seq""",
                {"sample": args.sample_rows, "total": total},
            )
        else:
            con.execute(
                "CREATE TABLE input_addresses AS SELECT address_id,address FROM baseline.source ORDER BY seq"
            )
        total = con.execute("SELECT count(*) FROM input_addresses").fetchone()[0]
        summary["input_rows"] = total
        summary["reference_rows"] = con.execute(
            "SELECT count(*) FROM baseline.wa_reference"
        ).fetchone()[0]
        summary["setup_seconds"] = time.perf_counter() - started
        profile_path = args.out / "profile.json"
        literal = str(profile_path).replace("'", "''")
        con.execute(f"SET profiling_output='{literal}'")
        con.execute("SET enable_profiling='json'")
        save()
        monitor.start()
        stage = "matching"
        print(
            f"START batch={args.batch_size}, memory={args.memory_limit}, rows={total:,}",
            flush=True,
        )
        tick = time.perf_counter()
        con.execute(f"""CREATE TABLE results AS SELECT * FROM wa_geocode_batched(
            'input_addresses','baseline.wa_reference','baseline.wa_index',batch_size:={args.batch_size})""")
        summary["matching_seconds"] = time.perf_counter() - tick
        summary["rows_per_second"] = total / summary["matching_seconds"]
        summary["matching_peak_process_rss_mib"] = peak_rss_mib()
        monitor.stop()
        con.execute("PRAGMA disable_profiling")
        profile = json.loads(profile_path.read_text())
        summary["profile_peak_buffer_bytes"] = profile["system_peak_buffer_memory"]
        summary["profile_peak_spill_bytes"] = profile["system_peak_temp_dir_size"]
        stage = "validation"
        validation_start = time.perf_counter()
        differences = con.execute("""WITH expected AS (
                SELECT b.* FROM baseline.results b JOIN input_addresses i USING(address_id)
            )
            SELECT count(*) FROM (
                (FROM results EXCEPT ALL FROM expected)
                UNION ALL (FROM expected EXCEPT ALL FROM results)
            )""").fetchone()[0]
        summary["rows_different_from_baseline"] = differences
        summary["result_rows"] = con.execute("SELECT count(*) FROM results").fetchone()[
            0
        ]
        if differences or summary["result_rows"] != total:
            raise AssertionError("Batched results differ from the baseline")
        con.execute("CHECKPOINT")
        summary["validation_and_checkpoint_seconds"] = (
            time.perf_counter() - validation_start
        )
        summary["status"] = "ok"
        print(
            f"DONE batch={args.batch_size}: {summary['matching_seconds']:.1f}s, "
            f"{summary['rows_per_second']:.1f} rows/s, "
            f"RSS {summary['matching_peak_process_rss_mib']:.0f} MiB, "
            f"spill {summary['profile_peak_spill_bytes'] / 1024**2:.1f} MiB, zero differences",
            flush=True,
        )
        return 0
    except Exception as exc:
        summary.update(
            status="error",
            failed_stage=stage,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        if stage == "matching" and tick is not None:
            summary["matching_seconds_before_error"] = time.perf_counter() - tick
        print(f"FAILED batch={args.batch_size}: {exc}", flush=True)
        return 2
    finally:
        if monitor.thread.is_alive():
            monitor.stop()
        summary["total_seconds"] = time.perf_counter() - started
        save()
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=[250, 500, 1000, 2000, 5000]
    )
    parser.add_argument(
        "--sample-rows", type=int, default=20000, help="0 for the entire corpus"
    )
    parser.add_argument("--memory-limit", default="1GB")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--batch-size", type=int)
    args = parser.parse_args()
    if args.sample_rows < 0 or args.threads < 1 or any(size < 1 for size in args.sizes):
        parser.error(
            "Sample rows must be nonnegative; sizes and threads must be positive"
        )
    if args.worker:
        if args.batch_size is None or args.batch_size < 1:
            parser.error("Worker requires a positive --batch-size")
        sys.exit(worker(args))
    args.out.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[3]
    summary = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True
            ).strip()
        ),
        "runner_sha256": sha256(Path(__file__)),
        "extension_sha256": sha256(args.extension),
        "database_sha256": sha256(args.database),
        "duckdb": duckdb.__version__,
        "platform": platform.platform(),
        "memory_limit": args.memory_limit,
        "threads": args.threads,
        "sample_rows_requested": args.sample_rows,
        "order": args.sizes,
        "measurement": "One fresh process per size; one SQL call; includes input snapshot, validation and result creation; excludes reference/index build; no warmup",
        "runs": [],
    }
    for size in args.sizes:
        out = args.out / f"batch-{size}"
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                str(Path(__file__).resolve()),
                "--worker",
                "--database",
                str(args.database.resolve()),
                "--extension",
                str(args.extension.resolve()),
                "--out",
                str(out.resolve()),
                "--batch-size",
                str(size),
                "--sample-rows",
                str(args.sample_rows),
                "--memory-limit",
                args.memory_limit,
                "--threads",
                str(args.threads),
            ],
            check=False,
        )
        run = (
            json.loads((out / "summary.json").read_text())
            if (out / "summary.json").exists()
            else {"status": "crashed"}
        )
        run["returncode"] = result.returncode
        summary["runs"].append(run)
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    sys.exit(0 if all(run["status"] == "ok" for run in summary["runs"]) else 2)


if __name__ == "__main__":
    main()
